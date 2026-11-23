from __future__ import annotations

import json
import threading
from dataclasses import dataclass
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

from sqlalchemy import desc, select
from sqlalchemy.orm import Session

from app.config import Settings
from app.models import (
    AuditLog,
    BlockingOperation,
    BlockingQueueItem,
    EmailLoginMapping,
    HRSourceRecord,
    OperationStatus,
)
from app.services.ad import ADDirectoryUser, ActiveDirectoryService
from app.services.blocking_queue import (
    BlockingOperationView,
    BlockingQueueService,
    BlockingTargetView,
)
from app.services.hr_registry import HRRegistryService
from app.services.itinvent import (
    ITInventEmployeeAssets,
    ITInventEquipment,
    ITInventService,
)
from app.services.itinvent_control import ITInventControlService
from app.services.zimbra import ZimbraAccountIdentity, ZimbraService


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


ITINVENT_SNAPSHOT_ACTION = "itinvent_last_success_snapshot"


@dataclass(frozen=True)
class BlockingCard:
    record_id: int
    worker_key: str
    source_id: str
    fio: str
    corporate_email: str
    placements: tuple[str, ...]
    effective_login: str
    ad_target_guid: str
    zimbra_target_id: str
    zimbra_target_email: str
    ad_user: ADDirectoryUser | None
    ad_error: str
    zimbra: ZimbraAccountIdentity | None
    zimbra_error: str
    zimbra_status_label: str
    itinvent: ITInventEmployeeAssets | None
    itinvent_state: str
    itinvent_error: str
    itinvent_checked_at: str

    @property
    def ad_is_blocked(self) -> bool:
        return self.ad_user is not None and not self.ad_user.is_enabled

    @property
    def zimbra_is_closed(self) -> bool:
        return bool(
            self.zimbra is not None
            and self.zimbra.account_status.strip().lower() == "closed"
        )

    @property
    def has_blocking_target(self) -> bool:
        return bool(
            self.effective_login
            or self.ad_target_guid
            or self.zimbra_target_email
            or self.zimbra_target_id
        )


@dataclass(frozen=True)
class BlockingITInventResult:
    effective_login: str
    itinvent: ITInventEmployeeAssets | None
    state: str
    error: str
    checked_at: str


@dataclass(frozen=True)
class BlockingResult:
    operation_id: int
    fio: str
    login: str
    corporate_email: str
    status: str
    status_label: str
    ad: BlockingTargetView | None
    zimbra: BlockingTargetView | None
    equipment_count: int
    dry_run: bool
    error_message: str

    @property
    def ad_disabled(self) -> bool:
        return bool(
            self.ad is not None
            and self.ad.status in {"completed", "already_completed"}
        )

    @property
    def zimbra_closed(self) -> bool:
        return bool(
            self.zimbra is not None
            and self.zimbra.status in {"completed", "already_completed"}
        )

    @property
    def zimbra_locked(self) -> bool:
        # Совместимость со старым шаблоном/кодом. Целевое состояние теперь closed.
        return self.zimbra_closed


class BlockingService:
    _locks_guard = threading.Lock()
    _locks: dict[str, threading.Lock] = {}

    def __init__(self, settings: Settings, db: Session):
        self.settings = settings
        self.db = db

    @classmethod
    def _lock_for(cls, key: str) -> threading.Lock:
        normalized = str(key or "").strip().lower()
        with cls._locks_guard:
            lock = cls._locks.get(normalized)
            if lock is None:
                lock = threading.Lock()
                cls._locks[normalized] = lock
            return lock

    @classmethod
    def _release_lock(cls, key: str, lock: threading.Lock) -> None:
        lock.release()
        normalized = str(key or "").strip().lower()
        with cls._locks_guard:
            current = cls._locks.get(normalized)
            if current is lock and not lock.locked():
                cls._locks.pop(normalized, None)

    def search(self, query: str, limit: int = 100) -> list[dict]:
        text = str(query or "").strip()
        if len(text) < 2:
            return []
        return HRRegistryService(self.settings, self.db).list_rows(
            query=text,
            status="all",
            limit=max(1, min(limit, 200)),
        )

    @staticmethod
    def _placements(record: HRSourceRecord) -> tuple[str, ...]:
        try:
            raw = json.loads(record.placements_json or "[]")
        except json.JSONDecodeError:
            raw = []
        result: list[str] = []
        for value in raw:
            department = str(value.get("department") or "").strip()
            position = str(value.get("position") or "").strip()
            label = " / ".join(part for part in (department, position) if part)
            if label:
                result.append(label)
        return tuple(result)

    def _mapping(self, record: HRSourceRecord) -> EmailLoginMapping | None:
        return self.db.scalar(
            select(EmailLoginMapping).where(
                EmailLoginMapping.worker_key == record.worker_key,
                EmailLoginMapping.source_domain == record.source_id,
            )
        )

    @staticmethod
    def _zimbra_label(identity: ZimbraAccountIdentity | None) -> str:
        if identity is None:
            return "Не найдена"
        labels = {
            "active": "Активна",
            "locked": "Заблокирована",
            "closed": "Закрыта",
            "maintenance": "Обслуживание",
        }
        return labels.get(
            identity.account_status,
            identity.account_status or "Существует",
        )

    def _checked_at(self) -> str:
        try:
            tz = ZoneInfo(self.settings.app_timezone)
        except Exception:
            tz = timezone.utc
        return datetime.now(tz).strftime("%d.%m.%Y %H:%M:%S")

    def _itinvent_lookup(self, login: str) -> BlockingITInventResult:
        normalized = str(login or "").strip().lower()
        itinvent_service = ITInventService(self.settings)
        if not self.settings.itinvent_enabled or not itinvent_service.configured:
            return BlockingITInventResult(
                effective_login=normalized,
                itinvent=None,
                state="not_configured",
                error="",
                checked_at="",
            )
        if not normalized:
            return BlockingITInventResult(
                effective_login="",
                itinvent=None,
                state="no_login",
                error="",
                checked_at="",
            )

        selection = ITInventControlService(self.settings, self.db).load()
        try:
            assets = itinvent_service.equipment_for_login(
                normalized,
                location_nos=selection.location_nos,
                equipment_types=selection.equipment_type_keys,
            )
            return BlockingITInventResult(
                effective_login=normalized,
                itinvent=assets,
                state="found" if assets.owner_found else "owner_not_found",
                error="",
                checked_at=self._checked_at(),
            )
        except Exception as exc:
            return BlockingITInventResult(
                effective_login=normalized,
                itinvent=None,
                state="error",
                error=str(exc),
                checked_at="",
            )

    @staticmethod
    def _itinvent_snapshot_target(record: HRSourceRecord) -> str:
        return f"{record.source_id}:{record.worker_key}"

    def _remember_itinvent(
        self,
        record: HRSourceRecord,
        result: BlockingITInventResult,
    ) -> None:
        if result.state not in {"found", "owner_not_found"}:
            return
        assets = result.itinvent
        payload = {
            "version": 1,
            "login": result.effective_login,
            "checked_at": result.checked_at,
            "owner_found": bool(assets is not None and assets.owner_found),
            "owner_display_name": (
                assets.owner_display_name if assets is not None else ""
            ),
            "owner_login": (
                assets.owner_login if assets is not None else result.effective_login
            ),
            "equipment": [
                {
                    "type": item.equipment_type,
                    "name": item.equipment_name,
                    "serial_number": item.serial_number,
                    "inventory_number": item.inventory_number,
                    "accounting_inventory_number": item.accounting_inventory_number,
                }
                for item in (assets.equipment if assets is not None else ())
            ],
        }
        self.db.add(
            AuditLog(
                actor="system",
                action=ITINVENT_SNAPSHOT_ACTION,
                target=self._itinvent_snapshot_target(record),
                result="success",
                details=json.dumps(
                    payload,
                    ensure_ascii=False,
                    sort_keys=True,
                ),
            )
        )
        self.db.commit()

    def _cached_itinvent(
        self,
        record: HRSourceRecord,
        login: str,
        error: str,
    ) -> BlockingITInventResult | None:
        normalized = str(login or "").strip().lower()
        event = self.db.scalar(
            select(AuditLog)
            .where(
                AuditLog.action == ITINVENT_SNAPSHOT_ACTION,
                AuditLog.target == self._itinvent_snapshot_target(record),
                AuditLog.result == "success",
            )
            .order_by(desc(AuditLog.id))
            .limit(1)
        )
        if event is None:
            return None
        try:
            payload = json.loads(event.details or "{}")
        except (TypeError, json.JSONDecodeError):
            return None
        if not isinstance(payload, dict):
            return None
        snapshot_login = str(payload.get("login") or "").strip().lower()
        if not snapshot_login or snapshot_login != normalized:
            return None

        equipment_values = payload.get("equipment") or []
        if not isinstance(equipment_values, list):
            equipment_values = []
        equipment: list[ITInventEquipment] = []
        for value in equipment_values:
            if not isinstance(value, dict):
                continue
            equipment.append(
                ITInventEquipment(
                    equipment_type=str(value.get("type") or ""),
                    equipment_name=str(value.get("name") or ""),
                    serial_number=str(value.get("serial_number") or ""),
                    inventory_number=str(value.get("inventory_number") or ""),
                    accounting_inventory_number=str(
                        value.get("accounting_inventory_number") or ""
                    ),
                )
            )

        assets = ITInventEmployeeAssets(
            owner_found=bool(payload.get("owner_found")),
            owner_display_name=str(payload.get("owner_display_name") or ""),
            owner_login=str(payload.get("owner_login") or snapshot_login),
            equipment=tuple(equipment),
        )
        return BlockingITInventResult(
            effective_login=normalized,
            itinvent=assets,
            state="stale",
            error=error,
            checked_at=str(payload.get("checked_at") or ""),
        )

    def _itinvent_login_for_record(self, record: HRSourceRecord) -> str:
        mapping = self._mapping(record)
        if mapping is not None and mapping.ad_login.strip():
            return mapping.ad_login.strip().lower()
        if record.login.strip():
            return record.login.strip().lower()
        if record.corporate_email.strip() and self.settings.zimbra_check_enabled:
            try:
                identity = ZimbraService(self.settings).account_by_address(
                    record.corporate_email
                )
                if identity is not None:
                    return identity.login.strip().lower()
            except Exception:
                pass
        return ""

    def refresh_itinvent(self, record_id: int) -> BlockingITInventResult:
        record = self.db.get(HRSourceRecord, record_id)
        if record is None or not record.is_present:
            raise LookupError("Работник не найден в текущем кадровом реестре")
        result = self._itinvent_lookup(self._itinvent_login_for_record(record))
        if result.state in {"found", "owner_not_found"}:
            self._remember_itinvent(record, result)
        return result

    def card(
        self,
        record_id: int,
        *,
        remember_itinvent: bool = True,
    ) -> BlockingCard:
        record = self.db.get(HRSourceRecord, record_id)
        if record is None or not record.is_present:
            raise LookupError("Работник не найден в текущем кадровом реестре")

        mapping = self._mapping(record)
        ad_user: ADDirectoryUser | None = None
        z_identity: ZimbraAccountIdentity | None = None
        ad_error = ""
        zimbra_error = ""

        zimbra_target_id = (
            mapping.zimbra_id.strip() if mapping is not None else ""
        )
        zimbra_target_email = (
            mapping.zimbra_email.strip().lower()
            if mapping is not None and mapping.zimbra_email.strip()
            else record.corporate_email.strip().lower()
        )

        zimbra_service = ZimbraService(self.settings)
        if zimbra_target_email and self.settings.zimbra_check_enabled:
            try:
                if zimbra_target_id:
                    z_identity = zimbra_service.accounts_by_ids(
                        [zimbra_target_id]
                    ).get(zimbra_target_id)
                if z_identity is None:
                    z_identity = zimbra_service.account_by_address(
                        zimbra_target_email
                    )
                if z_identity is not None:
                    zimbra_target_id = z_identity.zimbra_id or zimbra_target_id
                    zimbra_target_email = z_identity.primary_email
            except Exception as exc:
                zimbra_error = str(exc)

        candidate_login = ""
        ad_target_guid = (
            mapping.ad_object_guid.strip() if mapping is not None else ""
        )
        if mapping is not None and mapping.ad_login.strip():
            candidate_login = mapping.ad_login.strip().lower()
        elif z_identity is not None:
            candidate_login = z_identity.login.strip().lower()
        else:
            candidate_login = record.login.strip().lower()

        if self.settings.ad_check_enabled:
            try:
                ad_service = ActiveDirectoryService(self.settings)
                if ad_target_guid:
                    ad_user = ad_service.get_user_by_object_guid(ad_target_guid)
                if ad_user is None and candidate_login:
                    ad_user = ad_service.get_user(candidate_login)
                if ad_user is not None:
                    candidate_login = ad_user.username
                    ad_target_guid = ad_user.object_guid or ad_target_guid
            except Exception as exc:
                ad_error = str(exc)

        effective_login = ad_user.username if ad_user is not None else candidate_login
        itinvent_result = self._itinvent_lookup(effective_login)
        if (
            remember_itinvent
            and itinvent_result.state in {"found", "owner_not_found"}
        ):
            self._remember_itinvent(record, itinvent_result)
        elif itinvent_result.state == "error":
            cached = self._cached_itinvent(
                record,
                effective_login,
                itinvent_result.error,
            )
            if cached is not None:
                itinvent_result = cached

        return BlockingCard(
            record_id=record.id,
            worker_key=record.worker_key,
            source_id=record.source_id,
            fio=record.fio,
            corporate_email=record.corporate_email,
            placements=self._placements(record),
            effective_login=effective_login,
            ad_target_guid=ad_target_guid,
            zimbra_target_id=zimbra_target_id,
            zimbra_target_email=zimbra_target_email,
            ad_user=ad_user,
            ad_error=ad_error,
            zimbra=z_identity,
            zimbra_error=zimbra_error,
            zimbra_status_label=self._zimbra_label(z_identity),
            itinvent=itinvent_result.itinvent,
            itinvent_state=itinvent_result.state,
            itinvent_error=itinvent_result.error,
            itinvent_checked_at=itinvent_result.checked_at,
        )

    @staticmethod
    def _equipment_snapshot(card: BlockingCard) -> tuple[list, str]:
        equipment = list(card.itinvent.equipment) if card.itinvent is not None else []
        payload = json.dumps(
            [
                {
                    "type": item.equipment_type,
                    "name": item.equipment_name,
                    "serial_number": item.serial_number,
                    "inventory_number": item.inventory_number,
                    "accounting_inventory_number": item.accounting_inventory_number,
                }
                for item in equipment
            ],
            ensure_ascii=False,
            sort_keys=True,
        )
        return equipment, payload

    def _result_from_view(
        self,
        operation: BlockingOperation,
        view: BlockingOperationView,
    ) -> BlockingResult:
        return BlockingResult(
            operation_id=operation.id,
            fio=operation.full_name,
            login=operation.login,
            corporate_email=operation.corporate_email,
            status=view.status,
            status_label=view.status_label,
            ad=view.ad,
            zimbra=view.zimbra,
            equipment_count=operation.equipment_count,
            dry_run=operation.dry_run,
            error_message=view.error_message,
        )

    def _open_operation_for_worker(self, worker_key: str) -> BlockingOperation | None:
        return self.db.scalar(
            select(BlockingOperation)
            .join(
                BlockingQueueItem,
                BlockingQueueItem.operation_id == BlockingOperation.id,
            )
            .where(
                BlockingOperation.worker_key == worker_key,
                BlockingQueueItem.status.in_(["pending", "intervention"]),
            )
            .order_by(desc(BlockingOperation.created_at))
            .limit(1)
        )

    def operation_result(self, operation_id: int) -> BlockingResult:
        operation = self.db.get(BlockingOperation, operation_id)
        if operation is None:
            raise LookupError("Операция блокировки не найдена")
        view = BlockingQueueService(self.settings, self.db).view(operation_id)
        return self._result_from_view(operation, view)

    def latest_result_for_record(self, record_id: int) -> BlockingResult | None:
        operation = self.db.scalar(
            select(BlockingOperation)
            .join(
                BlockingQueueItem,
                BlockingQueueItem.operation_id == BlockingOperation.id,
            )
            .where(BlockingOperation.source_record_id == record_id)
            .order_by(desc(BlockingOperation.created_at))
            .limit(1)
        )
        if operation is None:
            return None
        return self.operation_result(operation.id)

    def block(self, record_id: int, operator: str) -> BlockingResult:
        first = self.card(record_id)
        lock_key = first.worker_key or first.effective_login or str(record_id)
        lock = self._lock_for(lock_key)
        if not lock.acquire(blocking=False):
            raise RuntimeError(
                "Блокировка этого работника уже выполняется. "
                "Дождитесь завершения текущей операции."
            )

        try:
            existing = self._open_operation_for_worker(first.worker_key)
            if existing is not None:
                view = BlockingQueueService(self.settings, self.db).process_operation(
                    existing.id,
                    force=True,
                )
                self.db.refresh(existing)
                return self._result_from_view(existing, view)

            # Повторно читаем внешние системы и IT Invent непосредственно перед
            # созданием задания. Если сервер недоступен, известные стабильные
            # идентификаторы все равно попадут в очередь и будут обработаны позже.
            # Для IT Invent при ошибке используется только последний успешно
            # сохраненный снимок с тем же доменным логином.
            card = self.card(record_id)
            equipment, equipment_snapshot = self._equipment_snapshot(card)
            operation = BlockingOperation(
                worker_key=card.worker_key,
                source_id=card.source_id,
                source_record_id=card.record_id,
                operator_username=operator,
                full_name=card.fio,
                login=card.effective_login,
                corporate_email=card.corporate_email,
                status=OperationStatus.RUNNING,
                dry_run=self.settings.dry_run,
                itinvent_checked=card.itinvent_state in {"found", "owner_not_found"},
                itinvent_owner_name=(
                    card.itinvent.owner_display_name if card.itinvent is not None else ""
                ),
                equipment_count=len(equipment),
                equipment_snapshot_json=equipment_snapshot,
            )
            self.db.add(operation)
            self.db.flush()

            self.db.add_all(
                [
                    BlockingQueueItem(
                        operation_id=operation.id,
                        system="ad",
                        target_identifier=card.effective_login,
                        stable_id=card.ad_target_guid,
                        desired_state="disabled",
                        status="pending",
                        next_attempt_at=utcnow(),
                    ),
                    BlockingQueueItem(
                        operation_id=operation.id,
                        system="zimbra",
                        target_identifier=(
                            card.zimbra.primary_email
                            if card.zimbra is not None
                            else card.zimbra_target_email
                        ),
                        stable_id=(
                            card.zimbra.zimbra_id
                            if card.zimbra is not None
                            else card.zimbra_target_id
                        ),
                        desired_state="closed",
                        status="pending",
                        next_attempt_at=utcnow(),
                    ),
                ]
            )
            self.db.add(
                AuditLog(
                    actor=operator,
                    action="block_accounts_requested",
                    target=card.effective_login or card.corporate_email,
                    result="accepted",
                    details=(
                        f"worker_key={card.worker_key}; "
                        f"AD={card.effective_login or card.ad_target_guid}; "
                        f"Zimbra={card.zimbra_target_email or card.zimbra_target_id}; "
                        f"ITInvent={len(equipment)}; "
                        f"ITInventState={card.itinvent_state}; "
                        f"dry_run={operation.dry_run}"
                    )[:1000],
                )
            )
            self.db.commit()
            self.db.refresh(operation)

            # Первая попытка всегда выполняется немедленно. Недоступные серверы
            # переводятся в pending и дальше обслуживаются фоновым worker'ом.
            view = BlockingQueueService(self.settings, self.db).process_operation(
                operation.id,
                force=True,
            )
            self.db.refresh(operation)
            return self._result_from_view(operation, view)
        except Exception:
            self.db.rollback()
            raise
        finally:
            self._release_lock(lock_key, lock)

    def retry_operation(self, operation_id: int) -> BlockingResult:
        view = BlockingQueueService(self.settings, self.db).process_operation(
            operation_id,
            force=True,
        )
        operation = self.db.get(BlockingOperation, operation_id)
        if operation is None:
            raise LookupError("Операция блокировки не найдена")
        return self._result_from_view(operation, view)
