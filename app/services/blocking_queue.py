from __future__ import annotations

import threading
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from app.config import Settings
from app.models import BlockingOperation, BlockingQueueItem, OperationStatus
from app.services.ad import ActiveDirectoryService
from app.services.zimbra import ZimbraAccountIdentity, ZimbraService


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


GOOD_STATUSES = {"completed", "already_completed"}
FINAL_STATUSES = GOOD_STATUSES | {"dry_run"}
RETRY_DELAYS_MINUTES = (1, 2, 5, 10, 15)


@dataclass(frozen=True)
class BlockingTargetView:
    system: str
    status: str
    status_label: str
    target_identifier: str
    attempts: int
    last_error: str
    last_result: str
    last_attempt_at: datetime | None
    next_attempt_at: datetime | None
    completed_at: datetime | None
    last_attempt_label: str
    next_attempt_label: str
    completed_label: str


@dataclass(frozen=True)
class BlockingOperationView:
    operation_id: int
    status: str
    status_label: str
    ad: BlockingTargetView | None
    zimbra: BlockingTargetView | None
    error_message: str
    completed_at: datetime | None
    dry_run: bool


class PermanentBlockingError(RuntimeError):
    pass


class BlockingQueueService:
    """Доводит AD и Zimbra до требуемого состояния с безопасными повторами."""

    _locks_guard = threading.Lock()
    _operation_locks: dict[int, threading.Lock] = {}

    def __init__(self, settings: Settings, db: Session):
        self.settings = settings
        self.db = db

    @classmethod
    def _operation_lock(cls, operation_id: int) -> threading.Lock:
        with cls._locks_guard:
            lock = cls._operation_locks.get(operation_id)
            if lock is None:
                lock = threading.Lock()
                cls._operation_locks[operation_id] = lock
            return lock

    @classmethod
    def _release_operation_lock(
        cls,
        operation_id: int,
        lock: threading.Lock,
    ) -> None:
        lock.release()
        with cls._locks_guard:
            current = cls._operation_locks.get(operation_id)
            if current is lock and not lock.locked():
                cls._operation_locks.pop(operation_id, None)

    @staticmethod
    def _is_temporary_error(exc: Exception) -> bool:
        text = str(exc or "").casefold()
        permanent_markers = (
            "не найд",
            "not found",
            "no such account",
            "invalid credential",
            "authentication failed",
            "permission denied",
            "access denied",
            "insufficient access",
            "недостаточно прав",
            "неверный пароль",
            "host key",
            "fingerprint",
            "bind failed",
            "bind не выполнен",
        )
        if any(marker in text for marker in permanent_markers):
            return False

        temporary_markers = (
            "timeout",
            "timed out",
            "connection refused",
            "connection reset",
            "connection aborted",
            "unable to connect",
            "cannot connect",
            "could not connect",
            "server unavailable",
            "service unavailable",
            "network is unreachable",
            "no route to host",
            "name or service not known",
            "temporary failure in name resolution",
            "connection closed",
            "соединен",
            "подключен",
            "недоступ",
            "таймаут",
            "время ожидания",
        )
        return any(marker in text for marker in temporary_markers)

    @staticmethod
    def _next_retry(attempts: int) -> datetime:
        index = max(0, min(attempts - 1, len(RETRY_DELAYS_MINUTES) - 1))
        return utcnow() + timedelta(minutes=RETRY_DELAYS_MINUTES[index])

    def _queue_items(self, operation_id: int) -> list[BlockingQueueItem]:
        return list(
            self.db.scalars(
                select(BlockingQueueItem)
                .where(BlockingQueueItem.operation_id == operation_id)
                .order_by(BlockingQueueItem.id)
            ).all()
        )

    def _format_datetime(self, value: datetime | None) -> str:
        if value is None:
            return ""
        if value.tzinfo is None:
            value = value.replace(tzinfo=timezone.utc)
        try:
            tz = ZoneInfo(self.settings.app_timezone)
        except Exception:
            tz = timezone.utc
        return value.astimezone(tz).strftime("%d.%m.%Y %H:%M:%S")

    @staticmethod
    def _target_label(item: BlockingQueueItem) -> str:
        if item.last_result == 'not_applicable':
            return 'Отсутствует'
        labels = {
            "ad": {
                "pending": "Ожидает блокировки",
                "completed": "Заблокирована системой",
                "already_completed": "На момент выполнения уже была заблокирована",
                "intervention": "Требует вмешательства",
                "dry_run": "DRY RUN – блокировка не выполнялась",
            },
            "zimbra": {
                "pending": "Ожидает закрытия",
                "completed": "Закрыта системой",
                "already_completed": "На момент выполнения уже была закрыта",
                "intervention": "Требует вмешательства",
                "dry_run": "DRY RUN – закрытие не выполнялось",
            },
        }
        return labels.get(item.system, {}).get(item.status, item.status)

    def view(self, operation_id: int) -> BlockingOperationView:
        operation = self.db.get(BlockingOperation, operation_id)
        if operation is None:
            raise LookupError("Операция блокировки не найдена")
        by_system = {item.system: item for item in self._queue_items(operation_id)}

        def target(system: str) -> BlockingTargetView | None:
            item = by_system.get(system)
            if item is None:
                return None
            return BlockingTargetView(
                system=system,
                status=item.status,
                status_label=self._target_label(item),
                target_identifier=item.target_identifier,
                attempts=item.attempts,
                last_error=item.last_error,
                last_result=item.last_result,
                last_attempt_at=item.last_attempt_at,
                next_attempt_at=item.next_attempt_at,
                completed_at=item.completed_at,
                last_attempt_label=self._format_datetime(item.last_attempt_at),
                next_attempt_label=self._format_datetime(item.next_attempt_at),
                completed_label=self._format_datetime(item.completed_at),
            )

        status_key = operation.status.value
        status_labels = {
            "running": "Ожидает завершения",
            "partial": "Частично выполнено",
            "success": "Выполнено",
            "failed": "Требует вмешательства",
        }
        return BlockingOperationView(
            operation_id=operation.id,
            status=status_key,
            status_label=(
                "DRY RUN"
                if operation.dry_run
                else status_labels.get(status_key, status_key)
            ),
            ad=target("ad"),
            zimbra=target("zimbra"),
            error_message=operation.error_message,
            completed_at=operation.completed_at,
            dry_run=operation.dry_run,
        )

    def _resolve_ad(self, item: BlockingQueueItem):
        service = ActiveDirectoryService(self.settings)
        user = None
        if item.stable_id.strip():
            user = service.get_user_by_object_guid(item.stable_id.strip())
        if user is None and not item.stable_id.strip() and item.target_identifier.strip():
            user = service.get_user(item.target_identifier.strip())
        if user is None:
            raise PermanentBlockingError("Учетная запись AD не найдена")
        return service, user

    def _resolve_zimbra(self, item: BlockingQueueItem) -> tuple[ZimbraService, ZimbraAccountIdentity]:
        service = ZimbraService(self.settings)
        identity = None
        if item.stable_id.strip():
            identity = service.accounts_by_ids([item.stable_id.strip()]).get(
                item.stable_id.strip()
            )
        if identity is None and not item.stable_id.strip() and item.target_identifier.strip():
            identity = service.account_by_address(item.target_identifier.strip())
        if identity is None:
            raise PermanentBlockingError("Учетная запись Zimbra не найдена")
        return service, identity

    def _complete_item(
        self,
        item: BlockingQueueItem,
        *,
        status: str,
        result: str,
    ) -> None:
        item.status = status
        item.last_error = ""
        item.last_result = result
        item.next_attempt_at = None
        item.completed_at = utcnow()

    def _fail_item(self, item: BlockingQueueItem, exc: Exception) -> None:
        item.last_error = str(exc)[:4000]
        item.last_result = "error"
        item.completed_at = None
        if isinstance(exc, PermanentBlockingError) or not self._is_temporary_error(exc):
            item.status = "intervention"
            item.next_attempt_at = None
        else:
            item.status = "pending"
            item.next_attempt_at = self._next_retry(item.attempts)

    def _process_ad(self, item: BlockingQueueItem) -> None:
        service, user = self._resolve_ad(item)
        item.target_identifier = user.username
        if user.object_guid:
            item.stable_id = user.object_guid
        if not user.is_enabled:
            self._complete_item(
                item,
                status="already_completed",
                result="already_disabled",
            )
            return
        if self.settings.dry_run:
            self._complete_item(item, status="dry_run", result="would_disable")
            return
        service.disable_user(user.username)
        self._complete_item(item, status="completed", result="disabled")

    def _process_zimbra(self, item: BlockingQueueItem) -> None:
        service, identity = self._resolve_zimbra(item)
        item.target_identifier = identity.primary_email
        if identity.zimbra_id:
            item.stable_id = identity.zimbra_id
        if identity.account_status.strip().lower() == "closed":
            self._complete_item(
                item,
                status="already_completed",
                result="already_closed",
            )
            return
        if self.settings.dry_run:
            self._complete_item(item, status="dry_run", result="would_close")
            return
        service.close_account(identity.primary_email)
        self._complete_item(item, status="completed", result="closed")

    def _process_item(self, item: BlockingQueueItem) -> None:
        item.attempts += 1
        item.last_attempt_at = utcnow()
        try:
            if not item.stable_id.strip() and not item.target_identifier.strip():
                self._complete_item(item, status='already_completed', result='not_applicable')
            elif item.system == "ad":
                self._process_ad(item)
            elif item.system == "zimbra":
                self._process_zimbra(item)
            else:
                raise PermanentBlockingError(
                    f"Неизвестная система блокировки: {item.system}"
                )
        except Exception as exc:
            self._fail_item(item, exc)

    def _refresh_operation(self, operation: BlockingOperation) -> None:
        items = self._queue_items(operation.id)
        if not items:
            operation.status = OperationStatus.FAILED
            operation.error_message = "Для операции не созданы задания AD/Zimbra"
            operation.completed_at = None
            return

        by_system = {item.system: item for item in items}
        ad_item = by_system.get("ad")
        z_item = by_system.get("zimbra")
        operation.ad_disabled = bool(
            ad_item is not None and ad_item.status in GOOD_STATUSES
        )
        operation.zimbra_locked = bool(
            z_item is not None and z_item.status in GOOD_STATUSES
        )

        pending = [item for item in items if item.status == "pending"]
        intervention = [item for item in items if item.status == "intervention"]
        successful = [item for item in items if item.status in FINAL_STATUSES]

        if pending:
            operation.status = (
                OperationStatus.PARTIAL if successful or intervention else OperationStatus.RUNNING
            )
            operation.completed_at = None
        elif intervention:
            operation.status = (
                OperationStatus.PARTIAL if successful else OperationStatus.FAILED
            )
            operation.completed_at = None
        else:
            operation.status = OperationStatus.SUCCESS
            operation.completed_at = utcnow()

        errors: list[str] = []
        for item in items:
            if item.status not in {"pending", "intervention"} or not item.last_error:
                continue
            prefix = "AD" if item.system == "ad" else "Zimbra"
            errors.append(f"{prefix}: {item.last_error}")
        operation.error_message = "\n".join(errors)[:4000]

    def process_operation(self, operation_id: int, *, force: bool = False) -> BlockingOperationView:
        lock = self._operation_lock(operation_id)
        if not lock.acquire(blocking=False):
            return self.view(operation_id)
        try:
            operation = self.db.get(BlockingOperation, operation_id)
            if operation is None:
                raise LookupError("Операция блокировки не найдена")
            now = utcnow()
            items = self._queue_items(operation_id)
            for item in items:
                eligible = item.status == "pending"
                if force and item.status == "intervention":
                    item.status = "pending"
                    item.next_attempt_at = now
                    eligible = True
                if not eligible:
                    continue
                if not force and item.next_attempt_at is not None:
                    due = item.next_attempt_at
                    if due.tzinfo is None:
                        due = due.replace(tzinfo=timezone.utc)
                    if due > now:
                        continue
                self._process_item(item)
                self.db.commit()

            self._refresh_operation(operation)
            self.db.commit()
            return self.view(operation_id)
        except Exception:
            self.db.rollback()
            raise
        finally:
            self._release_operation_lock(operation_id, lock)

    def process_due(self, *, limit: int = 20) -> int:
        now = utcnow()
        operation_ids = list(
            self.db.scalars(
                select(BlockingQueueItem.operation_id)
                .where(
                    BlockingQueueItem.status == "pending",
                    or_(
                        BlockingQueueItem.next_attempt_at.is_(None),
                        BlockingQueueItem.next_attempt_at <= now,
                    ),
                )
                .distinct()
                .limit(max(1, min(limit, 100)))
            ).all()
        )
        processed = 0
        for operation_id in operation_ids:
            self.process_operation(operation_id)
            processed += 1
        return processed
