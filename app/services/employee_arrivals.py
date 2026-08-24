from __future__ import annotations

import json
from collections import defaultdict
from datetime import datetime, timezone
from urllib.parse import urlencode

from sqlalchemy import desc, select
from sqlalchemy.orm import Session

from app.models import AuditLog, HRSourceRecord
from app.models_employee_arrivals import (
    HREmploymentArrivalEvent,
    HREmploymentArrivalSourceState,
)


NOT_REQUIRED_ACTION = "new_employment_not_required"


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def begin_arrival_source_sync(
    db: Session,
    *,
    source_id: str,
    source_name: str,
    has_existing_records: bool,
) -> bool:
    """Вернуть True, если текущая выгрузка должна стать тихой базовой точкой."""
    source_id = str(source_id or "").strip().lower()
    state = db.scalar(
        select(HREmploymentArrivalSourceState).where(
            HREmploymentArrivalSourceState.source_id == source_id
        )
    )
    if state is not None:
        state.source_name = source_name
        state.updated_at = utcnow()
        return False

    db.add(
        HREmploymentArrivalSourceState(
            source_id=source_id,
            source_name=source_name,
        )
    )
    # Для уже заполненного реестра механизм включается сразу: новые строки в
    # этой же выгрузке можно отличить от сохраненного штата. Совершенно новый
    # источник сначала только формирует базовый состав без сотен уведомлений.
    return not has_existing_records


def sync_employment_arrival(
    db: Session,
    *,
    worker_key: str,
    source_id: str,
    source_name: str,
    fio: str,
    is_present: bool,
    episode_started: bool,
    is_new_person: bool = False,
    baseline: bool = False,
    seen_at: datetime | None = None,
) -> HREmploymentArrivalEvent | None:
    """Обновить границу эпизода появления без привязки к учетным системам."""
    now = seen_at or utcnow()
    source_id = str(source_id or "").strip().lower()
    latest = db.scalar(
        select(HREmploymentArrivalEvent)
        .where(
            HREmploymentArrivalEvent.worker_key == worker_key,
            HREmploymentArrivalEvent.source_id == source_id,
        )
        .order_by(
            desc(HREmploymentArrivalEvent.sequence),
            desc(HREmploymentArrivalEvent.id),
        )
        .limit(1)
    )

    if not is_present:
        if latest is None or latest.ended_at is not None:
            return latest
        latest.source_name = source_name
        latest.fio = fio
        latest.ended_at = now
        latest.updated_at = now
        if latest.status == "pending":
            latest.status = "ended"
        return latest

    if episode_started:
        if baseline:
            return None
        if latest is not None and latest.ended_at is None:
            # Повторный вызов внутри одной транзакции/выгрузки не создает дубль.
            latest.source_name = source_name
            latest.fio = fio
            latest.last_seen_at = now
            latest.updated_at = now
            return latest
        event = HREmploymentArrivalEvent(
            worker_key=worker_key,
            source_id=source_id,
            source_name=source_name,
            sequence=1 if latest is None else latest.sequence + 1,
            fio=fio,
            arrival_kind="employee" if is_new_person else "employment",
            status="pending",
            first_seen_at=now,
            last_seen_at=now,
            updated_at=now,
        )
        db.add(event)
        return event

    if latest is not None and latest.ended_at is None:
        latest.source_name = source_name
        latest.fio = fio
        latest.last_seen_at = now
        latest.updated_at = now
    return latest


class EmployeeArrivalService:
    def __init__(self, db: Session):
        self.db = db

    @staticmethod
    def parse_event_ids(raw: str) -> list[int]:
        result: list[int] = []
        for value in str(raw or "").split(","):
            value = value.strip()
            if not value.isdigit():
                continue
            event_id = int(value)
            if event_id > 0 and event_id not in result:
                result.append(event_id)
        return result[:20]

    @staticmethod
    def _placements(record: HRSourceRecord) -> list[str]:
        try:
            rows = json.loads(record.placements_json or "[]")
        except (TypeError, json.JSONDecodeError):
            rows = []
        result: list[str] = []
        for row in rows if isinstance(rows, list) else []:
            if not isinstance(row, dict):
                continue
            position = str(row.get("position") or "").strip()
            department = str(row.get("department") or "").strip()
            label = " — ".join(value for value in (position, department) if value)
            if label and label not in result:
                result.append(label)
        return result

    def _pending_events(self, event_ids: list[int] | None = None) -> list[HREmploymentArrivalEvent]:
        query = select(HREmploymentArrivalEvent).where(
            HREmploymentArrivalEvent.status == "pending",
            HREmploymentArrivalEvent.ended_at.is_(None),
        )
        if event_ids is not None:
            if not event_ids:
                return []
            query = query.where(HREmploymentArrivalEvent.id.in_(event_ids))
        return list(
            self.db.scalars(
                query.order_by(
                    HREmploymentArrivalEvent.first_seen_at,
                    HREmploymentArrivalEvent.id,
                )
            ).all()
        )

    def list_pending(self, limit: int = 50) -> list[dict[str, object]]:
        events = self._pending_events()
        if not events:
            return []
        worker_keys = {event.worker_key for event in events}
        records = list(
            self.db.scalars(
                select(HRSourceRecord).where(
                    HRSourceRecord.worker_key.in_(worker_keys),
                    HRSourceRecord.is_present.is_(True),
                )
            ).all()
        )
        record_by_key = {
            (record.worker_key, record.source_id): record
            for record in records
        }
        grouped: dict[str, list[HREmploymentArrivalEvent]] = defaultdict(list)
        for event in events:
            if (event.worker_key, event.source_id) in record_by_key:
                grouped[event.worker_key].append(event)

        result: list[dict[str, object]] = []
        for worker_key, worker_events in grouped.items():
            worker_events.sort(key=lambda item: (item.first_seen_at, item.id))
            event_ids = [event.id for event in worker_events]
            current_records = [
                record_by_key[(worker_key, event.source_id)]
                for event in worker_events
            ]
            organizations = []
            positions: list[str] = []
            for event, record in zip(worker_events, current_records, strict=True):
                organizations.append(
                    {
                        "source_id": event.source_id,
                        "source_name": event.source_name or record.source_name or event.source_id,
                    }
                )
                for position in self._placements(record):
                    if position not in positions:
                        positions.append(position)
            personal_email = next(
                (
                    record.personal_email.strip()
                    for record in current_records
                    if record.personal_email.strip()
                ),
                "",
            )
            corporate_emails = list(
                dict.fromkeys(
                    record.corporate_email.strip()
                    for record in current_records
                    if record.corporate_email.strip()
                )
            )
            query = urlencode(
                {"arrival_event_ids": ",".join(str(value) for value in event_ids)}
            )
            result.append(
                {
                    "worker_key": worker_key,
                    "fio": next((event.fio for event in worker_events if event.fio), worker_key),
                    "kind": (
                        "employee"
                        if any(event.arrival_kind == "employee" for event in worker_events)
                        else "employment"
                    ),
                    "event_ids": event_ids,
                    "event_ids_value": ",".join(str(value) for value in event_ids),
                    "organizations": organizations,
                    "positions": positions,
                    "personal_email": personal_email,
                    "corporate_emails": corporate_emails,
                    "first_seen_at": min(event.first_seen_at for event in worker_events),
                    "registration_url": f"/employees/new?{query}",
                }
            )
        result.sort(key=lambda item: (item["first_seen_at"], str(item["fio"])))
        return result[: max(1, min(int(limit), 200))]

    def registration_context(self, raw_event_ids: str) -> dict[str, object]:
        event_ids = self.parse_event_ids(raw_event_ids)
        events = self._pending_events(event_ids)
        if not event_ids or {event.id for event in events} != set(event_ids):
            raise ValueError("Уведомление уже обработано или занятость больше не активна")
        worker_keys = {event.worker_key for event in events}
        if len(worker_keys) != 1:
            raise ValueError("В одной регистрации должен быть выбран один человек")
        records = list(
            self.db.scalars(
                select(HRSourceRecord).where(
                    HRSourceRecord.worker_key == events[0].worker_key,
                    HRSourceRecord.source_id.in_([event.source_id for event in events]),
                    HRSourceRecord.is_present.is_(True),
                )
            ).all()
        )
        if len(records) != len(events):
            raise ValueError("Кадровое состояние изменилось; обновите журнал")
        personal_email = next(
            (record.personal_email.strip() for record in records if record.personal_email.strip()),
            "",
        )
        preferred_domain = next(
            (event.source_id for event in events if event.source_id),
            "",
        )
        return {
            "event_ids": event_ids,
            "event_ids_value": ",".join(str(value) for value in event_ids),
            "worker_key": events[0].worker_key,
            "fio": events[0].fio,
            "personal_email": personal_email,
            "preferred_domain": preferred_domain,
            "events": events,
            "records": records,
            "corporate_emails": list(
                dict.fromkeys(
                    record.corporate_email.strip().lower()
                    for record in records
                    if record.corporate_email.strip()
                )
            ),
            "logins": list(
                dict.fromkeys(
                    record.login.strip().lower()
                    for record in records
                    if record.login.strip()
                )
            ),
        }

    def mark_not_required(
        self,
        raw_event_ids: str,
        *,
        operator: str,
    ) -> dict[str, object]:
        context = self.registration_context(raw_event_ids)
        now = utcnow()
        events = context["events"]
        organizations = []
        for event in events:
            event.status = "not_required"
            event.decision_by = operator
            event.decision_details = "Учетные записи не требуются для текущего эпизода занятости"
            event.decided_at = now
            event.updated_at = now
            organizations.append(
                {
                    "source_id": event.source_id,
                    "source_name": event.source_name or event.source_id,
                    "sequence": event.sequence,
                }
            )
        self.db.add(
            AuditLog(
                actor=operator,
                action=NOT_REQUIRED_ACTION,
                target=str(context["worker_key"]),
                result="success",
                details=json.dumps(
                    {
                        "fio": context["fio"],
                        "event_ids": context["event_ids"],
                        "organizations": organizations,
                        "scope": "current_employment_episode",
                    },
                    ensure_ascii=False,
                    sort_keys=True,
                ),
            )
        )
        self.db.commit()
        return context

    def mark_registered(
        self,
        raw_event_ids: str,
        *,
        operator: str,
        provisioning_operation_id: int,
    ) -> None:
        event_ids = self.parse_event_ids(raw_event_ids)
        if not event_ids:
            return
        events = list(
            self.db.scalars(
                select(HREmploymentArrivalEvent).where(
                    HREmploymentArrivalEvent.id.in_(event_ids)
                )
            ).all()
        )
        if {event.id for event in events} != set(event_ids):
            raise ValueError("Не удалось связать регистрацию с кадровым появлением")
        now = utcnow()
        for event in events:
            event.status = "registered"
            event.decision_by = operator
            event.decision_details = "Создание учетных записей запущено оператором"
            event.provisioning_operation_id = provisioning_operation_id
            event.decided_at = now
            event.updated_at = now
        self.db.commit()

    def mark_accounts_resolved(
        self,
        raw_event_ids: str,
        *,
        operator: str,
        decision_details: str,
        provisioning_operation_id: int | None = None,
    ) -> None:
        """Закрыть кадровое появление после принятия существующих учёток."""

        context = self.registration_context(raw_event_ids)
        now = utcnow()
        status = (
            "registered"
            if provisioning_operation_id is not None
            else "accounts_confirmed"
        )
        for event in context["events"]:
            event.status = status
            event.decision_by = operator
            event.decision_details = decision_details
            event.provisioning_operation_id = provisioning_operation_id
            event.decided_at = now
            event.updated_at = now
        self.db.add(
            AuditLog(
                actor=operator,
                action="new_employment_accounts_resolved",
                target=str(context["worker_key"]),
                result=status,
                details=json.dumps(
                    {
                        "fio": context["fio"],
                        "event_ids": context["event_ids"],
                        "decision": decision_details,
                        "provisioning_operation_id": (
                            provisioning_operation_id
                        ),
                    },
                    ensure_ascii=False,
                    sort_keys=True,
                ),
            )
        )
        self.db.commit()
