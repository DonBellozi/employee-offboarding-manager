from __future__ import annotations

import hashlib
import logging
import threading
from dataclasses import dataclass
from datetime import date, datetime, timezone
from zoneinfo import ZoneInfo

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import Settings
from app.models import EmailLoginMapping, HRSourceRecord, OneCImportRun
from app.models_dismissals import DismissalDeferral
from app.models_onec_sources import HREmploymentState
from app.models_zimbra_lifecycle import (
    ZimbraEmploymentAction,
    ZimbraLifecycleSettings,
)
from app.services.blocking_window import is_block_window_open
from app.services.onec_freshness import OneCSourceFreshnessService
from app.services.zimbra import ZimbraAccountIdentity, ZimbraService


logger = logging.getLogger(__name__)
POLL_SECONDS = 60
ACTIVE_STATUSES = {"active", "scheduled"}
OPEN_STATUSES = {"pending", "awaiting_permission", "intervention", "failed"}


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def normalize(value: str) -> str:
    return str(value or "").strip().lower()


def address_domain(value: str) -> str:
    address = normalize(value)
    return address.rsplit("@", 1)[1] if address.count("@") == 1 else ""


def address_login(value: str) -> str:
    address = normalize(value)
    return address.split("@", 1)[0] if address.count("@") == 1 else ""


@dataclass(frozen=True)
class ZimbraEmploymentSpec:
    plan_key: str
    worker_key: str
    fio: str
    zimbra_id: str
    action: str
    source_id: str
    target_address: str
    replacement_address: str
    dismissal_date: date
    effective_action_date: date
    details: str


class ZimbraEmploymentLifecycleService:
    """Интерпретирует кадровые состояния отдельно для каждого zimbraId."""

    def __init__(self, settings: Settings, db: Session):
        self.settings = settings
        self.db = db

    @property
    def local_now(self) -> datetime:
        return datetime.now(ZoneInfo(self.settings.app_timezone))

    def _ready(self) -> bool:
        importing = bool(
            self.db.scalar(
                select(OneCImportRun.id)
                .where(OneCImportRun.status == "running")
                .limit(1)
            )
        )
        if importing:
            return False
        return OneCSourceFreshnessService(
            self.settings, self.db
        ).all_control_exports_ready(expected_date=self.local_now.date())

    def _deferrals(self) -> dict[tuple[str, date], date]:
        return {
            (row.worker_key, row.dismissal_date): row.deferred_until
            for row in self.db.scalars(select(DismissalDeferral)).all()
        }

    def _effective_date(
        self,
        state: HREmploymentState,
        deferrals: dict[tuple[str, date], date],
    ) -> date:
        dismissal_date = state.dismissal_date or self.local_now.date()
        return max(
            dismissal_date,
            deferrals.get((state.worker_key, dismissal_date), dismissal_date),
        )

    def _due(
        self,
        state: HREmploymentState,
        deferrals: dict[tuple[str, date], date],
    ) -> bool:
        if state.status in ACTIVE_STATUSES:
            return False
        effective = self._effective_date(state, deferrals)
        today = self.local_now.date()
        if effective < today:
            return True
        if effective > today:
            return False
        if state.status_reason == "absent_from_export":
            return True
        dismissal_date = state.dismissal_date
        if dismissal_date is not None and dismissal_date < today:
            return True
        return is_block_window_open(self.local_now)

    @staticmethod
    def _key(
        worker_key: str,
        zimbra_id: str,
        action: str,
        target: str,
        dismissal_date: date,
    ) -> str:
        raw = "|".join(
            [worker_key, zimbra_id, action, target, dismissal_date.isoformat()]
        )
        return hashlib.sha256(raw.encode("utf-8")).hexdigest()

    def _spec(
        self,
        *,
        worker_key: str,
        fio: str,
        identity: ZimbraAccountIdentity,
        action: str,
        state: HREmploymentState,
        target: str,
        replacement: str = "",
        deferrals: dict[tuple[str, date], date],
        details: str,
    ) -> ZimbraEmploymentSpec:
        dismissal_date = state.dismissal_date or self.local_now.date()
        return ZimbraEmploymentSpec(
            plan_key=self._key(
                worker_key,
                identity.zimbra_id,
                action,
                target,
                dismissal_date,
            ),
            worker_key=worker_key,
            fio=fio,
            zimbra_id=identity.zimbra_id,
            action=action,
            source_id=normalize(state.source_id),
            target_address=normalize(target),
            replacement_address=normalize(replacement),
            dismissal_date=dismissal_date,
            effective_action_date=self._effective_date(state, deferrals),
            details=details,
        )

    def _specs_for_mailbox(
        self,
        *,
        worker_key: str,
        fio: str,
        identity: ZimbraAccountIdentity,
        states: dict[str, HREmploymentState],
        deferrals: dict[tuple[str, date], date],
        address_sources: dict[str, str] | None = None,
    ) -> list[ZimbraEmploymentSpec]:
        addresses = tuple(dict.fromkeys(normalize(v) for v in identity.addresses if v))
        primary = normalize(identity.primary_email)
        if primary and primary not in addresses:
            addresses = (primary, *addresses)

        due_by_address: dict[str, HREmploymentState] = {}
        active_addresses: list[str] = []
        unknown_addresses: list[str] = []
        address_sources = {
            normalize(address): normalize(source_id)
            for address, source_id in (address_sources or {}).items()
            if normalize(address) and normalize(source_id)
        }
        for address in addresses:
            source_id = address_sources.get(
                address,
                address_domain(address),
            )
            state = states.get(source_id)
            if state is None:
                unknown_addresses.append(address)
            elif self._due(state, deferrals):
                due_by_address[address] = state
            else:
                active_addresses.append(address)

        if not due_by_address:
            return []

        primary_state = due_by_address.get(primary)
        if primary_state is not None:
            replacement = next(
                (address for address in active_addresses if address != primary),
                "",
            )
            if replacement:
                return [
                    self._spec(
                        worker_key=worker_key,
                        fio=fio,
                        identity=identity,
                        action="transition",
                        state=primary_state,
                        target=primary,
                        replacement=replacement,
                        deferrals=deferrals,
                        details=(
                            "Основной адрес относится к завершенной занятости, "
                            "но alias того же физического ящика остается активным. "
                            "Требуется backup и решение оператора о смене primary."
                        ),
                    )
                ]
            if unknown_addresses:
                return [
                    self._spec(
                        worker_key=worker_key,
                        fio=fio,
                        identity=identity,
                        action="manual_review",
                        state=primary_state,
                        target=primary,
                        deferrals=deferrals,
                        details=(
                            "Основной адрес уволен, но у физического ящика есть "
                            "адреса без однозначной кадровой организации."
                        ),
                    )
                ]
            return [
                self._spec(
                    worker_key=worker_key,
                    fio=fio,
                    identity=identity,
                    action="close",
                    state=primary_state,
                    target=primary,
                    deferrals=deferrals,
                    details="У физического ящика не осталось действующих адресов организаций.",
                )
            ]

        result: list[ZimbraEmploymentSpec] = []
        for address, state in sorted(due_by_address.items()):
            result.append(
                self._spec(
                    worker_key=worker_key,
                    fio=fio,
                    identity=identity,
                    action="remove_alias",
                    state=state,
                    target=address,
                    deferrals=deferrals,
                    details=(
                        "Удаляется только адрес завершенной организации; "
                        "физический ящик и остальные адреса сохраняются."
                    ),
                )
            )
        return result

    @staticmethod
    def _address_sources_for_mailbox(
        *,
        identity: ZimbraAccountIdentity,
        mappings: list[EmailLoginMapping],
        states: dict[str, HREmploymentState],
        records: list[HRSourceRecord],
    ) -> dict[str, str]:
        """Связать legacy-адреса Zimbra с текущими кадровыми доменами.

        При переименовании домена source_id уже новый, а primary/alias в
        Zimbra может остаться старым. Стабильный zimbraId и явное
        сопоставление позволяют считать эти адреса одной занятостью.
        """

        state_sources = set(states)
        result: dict[str, str] = {}
        logins_by_source: dict[str, set[str]] = {}

        records_by_source: dict[str, list[HRSourceRecord]] = {}
        for record in records:
            source_id = normalize(record.source_id)
            if source_id in state_sources:
                records_by_source.setdefault(source_id, []).append(record)
                address = normalize(record.corporate_email)
                if address:
                    result[address] = source_id
                    login = address_login(address)
                    if login:
                        logins_by_source.setdefault(source_id, set()).add(login)

        for mapping in mappings:
            mapping_addresses = {
                normalize(mapping.source_email),
                normalize(mapping.zimbra_email),
            }
            mapping_addresses.discard("")
            candidates = [
                normalize(mapping.source_domain),
                *[address_domain(value) for value in mapping_addresses],
            ]
            source_id = next(
                (value for value in candidates if value in state_sources),
                "",
            )

            if not source_id:
                mapping_logins = {
                    address_login(value)
                    for value in mapping_addresses
                    if address_login(value)
                }
                matched_sources = {
                    candidate_source
                    for candidate_source, source_records in records_by_source.items()
                    for record in source_records
                    if (
                        normalize(record.corporate_email) in mapping_addresses
                        or address_login(record.corporate_email)
                        in mapping_logins
                    )
                }
                if len(matched_sources) == 1:
                    source_id = next(iter(matched_sources))

            # Для единственной занятости и единственного физического ящика
            # это безопасный fallback миграции домена. При нескольких
            # организациях остаются только точные связи выше.
            if not source_id and len(state_sources) == 1 and len(mappings) == 1:
                source_id = next(iter(state_sources))
            if not source_id:
                continue

            for address in mapping_addresses:
                result[address] = source_id
                login = address_login(address)
                if login:
                    logins_by_source.setdefault(source_id, set()).add(login)
            for record in records_by_source.get(source_id, []):
                address = normalize(record.corporate_email)
                if address:
                    result[address] = source_id
                    login = address_login(address)
                    if login:
                        logins_by_source.setdefault(source_id, set()).add(login)

        identity_addresses = {
            normalize(identity.primary_email),
            *(normalize(value) for value in identity.addresses),
        }
        identity_addresses.discard("")
        for address in identity_addresses:
            if address in result:
                continue
            domain = address_domain(address)
            if domain in state_sources:
                result[address] = domain
                continue
            login = address_login(address)
            matching_sources = {
                source_id
                for source_id, source_logins in logins_by_source.items()
                if login and login in source_logins
            }
            if len(matching_sources) == 1:
                result[address] = next(iter(matching_sources))
        return result

    def _current_specs(self) -> list[ZimbraEmploymentSpec]:
        states = list(self.db.scalars(select(HREmploymentState)).all())
        if not states:
            return []

        deferrals = self._deferrals()
        worker_keys = sorted(
            {
                row.worker_key
                for row in states
                if self._due(row, deferrals)
            }
        )
        if not worker_keys:
            return []
        worker_key_set = set(worker_keys)
        states = [
            row for row in states if row.worker_key in worker_key_set
        ]
        mappings = list(
            self.db.scalars(
                select(EmailLoginMapping).where(
                    EmailLoginMapping.worker_key.in_(worker_keys)
                )
            ).all()
        )
        records = list(
            self.db.scalars(
                select(HRSourceRecord).where(
                    HRSourceRecord.worker_key.in_(worker_keys)
                )
            ).all()
        )

        mappings_by_worker: dict[str, list[EmailLoginMapping]] = {}
        for mapping in mappings:
            mappings_by_worker.setdefault(mapping.worker_key, []).append(mapping)

        records_by_worker: dict[str, list[HRSourceRecord]] = {}
        for record in records:
            records_by_worker.setdefault(record.worker_key, []).append(record)

        # Явное сопоставление хранится только для исключений. Для обычных
        # одноименных AD/Zimbra учеток оно может быть удалено как избыточное,
        # поэтому кадровый lifecycle обязан уметь найти физический ящик также
        # по адресу из 1С. Дополнительные домены позволяют пережить смену
        # primary .com -> .ru (или обратную) без зависимости от старого e-mail.
        candidate_addresses_by_worker: dict[str, set[str]] = {}
        configured_domains = {
            normalize(value)
            for value in getattr(self.settings, "zimbra_domains", ())
            if normalize(value)
        }
        for worker_key in worker_keys:
            addresses = candidate_addresses_by_worker.setdefault(
                worker_key, set()
            )
            for record in records_by_worker.get(worker_key, []):
                address = normalize(record.corporate_email)
                if address:
                    addresses.add(address)
            for mapping in mappings_by_worker.get(worker_key, []):
                addresses.update(
                    value
                    for value in (
                        normalize(mapping.source_email),
                        normalize(mapping.zimbra_email),
                    )
                    if value
                )

        # Один и тот же local-part может принадлежать разным людям в разных
        # организациях. Междоменный поиск безопасен только для логина, который
        # в кадровых данных относится ровно к одному worker_key.
        workers_by_login: dict[str, set[str]] = {}
        owner_rows = self.db.execute(
            select(
                HRSourceRecord.worker_key,
                HRSourceRecord.corporate_email,
            )
        ).all()
        for worker_key, address in owner_rows:
            login = address_login(address)
            if login:
                workers_by_login.setdefault(login, set()).add(worker_key)
        mapping_owner_rows = self.db.execute(
            select(
                EmailLoginMapping.worker_key,
                EmailLoginMapping.source_email,
                EmailLoginMapping.zimbra_email,
            )
        ).all()
        for worker_key, source_email, zimbra_email in mapping_owner_rows:
            for address in (source_email, zimbra_email):
                login = address_login(address)
                if login:
                    workers_by_login.setdefault(login, set()).add(worker_key)
        for worker_key, addresses in candidate_addresses_by_worker.items():
            logins = {address_login(value) for value in tuple(addresses)}
            for login in logins:
                if (
                    not login
                    or len(workers_by_login.get(login, set())) != 1
                ):
                    continue
                addresses.update(
                    f"{login}@{domain}" for domain in configured_domains
                )

        service = ZimbraService(self.settings)
        zimbra_ids = sorted(
            {
                normalize(row.zimbra_id)
                for row in mappings
                if normalize(row.zimbra_id)
            }
        )
        identities_by_id = (
            service.accounts_by_ids(zimbra_ids) if zimbra_ids else {}
        )
        identities_by_id = {
            normalize(key): value for key, value in identities_by_id.items()
        }
        candidate_addresses = sorted(
            {
                address
                for addresses in candidate_addresses_by_worker.values()
                for address in addresses
            }
        )
        identities_by_address = (
            service.accounts_by_addresses(candidate_addresses)
            if candidate_addresses
            else {}
        )

        states_by_worker: dict[str, dict[str, HREmploymentState]] = {}
        for state in states:
            states_by_worker.setdefault(state.worker_key, {})[
                normalize(state.source_id)
            ] = state
        fio_by_worker: dict[str, str] = {}
        for row in [*states, *records]:
            if row.worker_key not in fio_by_worker and str(row.fio or "").strip():
                fio_by_worker[row.worker_key] = str(row.fio).strip()

        result: list[ZimbraEmploymentSpec] = []
        for worker_key in worker_keys:
            worker_mappings = mappings_by_worker.get(worker_key, [])
            identities: dict[str, ZimbraAccountIdentity] = {}
            for mapping in worker_mappings:
                zimbra_id = normalize(mapping.zimbra_id)
                identity = identities_by_id.get(zimbra_id)
                if identity is not None:
                    identities[normalize(identity.zimbra_id)] = identity
            for address in candidate_addresses_by_worker.get(worker_key, set()):
                identity = identities_by_address.get(address)
                if identity is not None:
                    identities[normalize(identity.zimbra_id)] = identity

            worker_states = states_by_worker.get(worker_key, {})
            worker_records = records_by_worker.get(worker_key, [])
            for identity in identities.values():
                identity_addresses = {
                    normalize(identity.primary_email),
                    *(normalize(value) for value in identity.addresses),
                }
                identity_addresses.discard("")
                relevant_mappings = [
                    mapping
                    for mapping in worker_mappings
                    if (
                        normalize(mapping.zimbra_id)
                        == normalize(identity.zimbra_id)
                        or bool(
                            identity_addresses
                            & {
                                normalize(mapping.source_email),
                                normalize(mapping.zimbra_email),
                            }
                        )
                    )
                ]
                result.extend(
                    self._specs_for_mailbox(
                        worker_key=worker_key,
                        fio=fio_by_worker.get(worker_key, ""),
                        identity=identity,
                        states=worker_states,
                        deferrals=deferrals,
                        address_sources=self._address_sources_for_mailbox(
                            identity=identity,
                            mappings=relevant_mappings,
                            states=worker_states,
                            records=worker_records,
                        ),
                    )
                )
        return result

    def _ensure_action(self, spec: ZimbraEmploymentSpec) -> ZimbraEmploymentAction:
        row = self.db.scalar(
            select(ZimbraEmploymentAction).where(
                ZimbraEmploymentAction.plan_key == spec.plan_key
            )
        )
        if row is None:
            row = ZimbraEmploymentAction(plan_key=spec.plan_key)
            self.db.add(row)
        row.worker_key = spec.worker_key
        row.fio = spec.fio
        row.zimbra_id = spec.zimbra_id
        row.action = spec.action
        row.source_id = spec.source_id
        row.target_address = spec.target_address
        row.replacement_address = spec.replacement_address
        row.dismissal_date = spec.dismissal_date
        row.effective_action_date = spec.effective_action_date
        row.details = spec.details
        row.updated_at = utcnow()
        if row.status == "cancelled":
            row.status = "pending"
            row.cancelled_at = None
        return row

    def _cancel_stale(self, current_keys: set[str]) -> None:
        rows = list(
            self.db.scalars(
                select(ZimbraEmploymentAction).where(
                    ZimbraEmploymentAction.status.in_(OPEN_STATUSES)
                )
            ).all()
        )
        for row in rows:
            if row.plan_key in current_keys:
                continue
            row.status = "cancelled"
            row.cancelled_at = utcnow()
            row.last_error = "Кадровое состояние или состав адресов Zimbra изменились"
            row.updated_at = utcnow()

    def _settings(self) -> ZimbraLifecycleSettings:
        row = self.db.get(ZimbraLifecycleSettings, 1)
        if row is None:
            row = ZimbraLifecycleSettings(id=1)
            self.db.add(row)
            self.db.commit()
            self.db.refresh(row)
        return row

    def _execute(self, row: ZimbraEmploymentAction) -> bool:
        current = {spec.plan_key: spec for spec in self._current_specs()}
        if row.plan_key not in current or not self._ready():
            row.status = "cancelled"
            row.cancelled_at = utcnow()
            row.last_error = "Повторная HR-проверка отменила действие"
            return False

        if row.action in {"transition", "manual_review"}:
            row.status = "intervention"
            return False
        config = self._settings()
        if row.action == "remove_alias" and not config.allow_alias_remove:
            row.status = "awaiting_permission"
            return False

        service = ZimbraService(self.settings)
        identity = service.accounts_by_ids([row.zimbra_id]).get(row.zimbra_id)
        row.attempts = int(row.attempts or 0) + 1
        row.last_attempt_at = utcnow()
        if identity is None:
            row.status = "failed"
            row.last_error = "Физический ящик Zimbra не найден перед действием"
            return False

        try:
            if row.action == "close":
                if normalize(identity.account_status) != "closed":
                    service.close_account(identity.primary_email)
                verified = service.accounts_by_ids([row.zimbra_id]).get(row.zimbra_id)
                if verified is None or normalize(verified.account_status) != "closed":
                    raise RuntimeError("Статус closed не подтвержден после команды")
            elif row.action == "remove_alias":
                if row.target_address in {normalize(v) for v in identity.addresses}:
                    service.remove_alias(identity.primary_email, row.target_address)
                verified = service.accounts_by_ids([row.zimbra_id]).get(row.zimbra_id)
                if verified is not None and row.target_address in {
                    normalize(v) for v in verified.addresses
                }:
                    raise RuntimeError("Удаление alias не подтверждено после команды")
            else:
                raise RuntimeError(f"Неизвестное действие Zimbra: {row.action}")
            row.status = "completed"
            row.completed_at = utcnow()
            row.last_error = ""
            return True
        except Exception as exc:
            row.status = "failed"
            row.last_error = str(exc)[:4000]
            return False

    def process(self) -> dict[str, int | str]:
        if not self._ready():
            return {"status": "sources_not_ready", "planned": 0, "completed": 0}
        specs = self._current_specs()
        rows = [self._ensure_action(spec) for spec in specs]
        self._cancel_stale({spec.plan_key for spec in specs})
        self.db.commit()

        completed = 0
        for row in rows:
            if row.status == "completed":
                continue
            if self._execute(row):
                completed += 1
            self.db.commit()
        return {"status": "ok", "planned": len(specs), "completed": completed}

    def open_actions(self, limit: int = 100) -> list[ZimbraEmploymentAction]:
        return list(
            self.db.scalars(
                select(ZimbraEmploymentAction)
                .where(ZimbraEmploymentAction.status.in_(OPEN_STATUSES))
                .order_by(
                    ZimbraEmploymentAction.updated_at.desc(),
                    ZimbraEmploymentAction.id.desc(),
                )
                .limit(max(1, min(limit, 500)))
            ).all()
        )

    def recent_actions(self, limit: int = 100) -> list[ZimbraEmploymentAction]:
        return list(
            self.db.scalars(
                select(ZimbraEmploymentAction)
                .order_by(
                    ZimbraEmploymentAction.updated_at.desc(),
                    ZimbraEmploymentAction.id.desc(),
                )
                .limit(max(1, min(limit, 500)))
            ).all()
        )


class ZimbraEmploymentLifecycleWorker:
    def __init__(self, settings: Settings, session_factory):
        self.settings = settings
        self.session_factory = session_factory
        self._stop_event = threading.Event()
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        if self._thread is not None and self._thread.is_alive():
            return
        self._thread = threading.Thread(
            target=self._run_loop,
            name="zimbra-employment-lifecycle",
            daemon=True,
        )
        self._thread.start()

    def stop(self) -> None:
        self._stop_event.set()
        if self._thread is not None:
            self._thread.join(timeout=5)

    def _run_once(self) -> None:
        with self.session_factory() as db:
            try:
                result = ZimbraEmploymentLifecycleService(
                    self.settings, db
                ).process()
                if result.get("completed"):
                    logger.info(
                        "Кадровый Zimbra lifecycle: выполнено %s",
                        result["completed"],
                    )
            except Exception:
                db.rollback()
                logger.exception("Ошибка кадрового Zimbra lifecycle")

    def _run_loop(self) -> None:
        while not self._stop_event.wait(POLL_SECONDS):
            self._run_once()
