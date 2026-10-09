from __future__ import annotations

import hashlib
import json
from datetime import timezone

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import AuditLog, HRSourceRecord, OneCImportRun
from app.models_account_requirement import (
    AccountRequirementArrival, AccountRequirementCase, AccountRequirementDecisionEvent, utcnow,
)
from app.models_employee_arrivals import HREmploymentArrivalEvent
from app.models_onec_sources import HREmploymentState
from app.services.employee_arrivals import EmployeeArrivalService

SNAPSHOT_SCHEMA_VERSION = 1
STATE_LABELS = {"pending": "Решение не принято", "clarification": "Требуется уточнение",
                "decided": "Решение принято", "cancelled": "Отменено"}
DECISION_LABELS = {"required": "Нужна", "not_required": "Не нужна"}


def canonical_json(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def snapshot_hash(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


class AccountRequirementService:
    def __init__(self, db: Session):
        self.db = db

    def assert_import_idle(self) -> None:
        # Imports commit several stages. Never label an intermediate HR state.
        if self.db.scalar(select(OneCImportRun.id).where(OneCImportRun.status == "running").limit(1)):
            raise ValueError("Дождитесь завершения кадрового импорта")

    def snapshot(self, worker_key: str) -> dict:
        records = self.db.scalars(select(HRSourceRecord).where(
            HRSourceRecord.worker_key == worker_key, HRSourceRecord.is_present.is_(True),
        ).order_by(HRSourceRecord.source_id)).all()
        states = {row.source_id: row for row in self.db.scalars(select(HREmploymentState).where(
            HREmploymentState.worker_key == worker_key,
        )).all()}
        placements = []
        for record in records:
            state = states.get(record.source_id)
            if state is not None and (not state.is_present or state.status not in {"active", "scheduled"}):
                continue
            try:
                rows = json.loads(record.placements_json or "[]")
            except (TypeError, ValueError):
                rows = []
            if not isinstance(rows, list):
                rows = []
            for row in rows or [{}]:
                if not isinstance(row, dict):
                    continue
                # Explicit allowlist: never copy raw HR JSON / emails / identifiers.
                placements.append({
                    "organization": str((state.source_name if state else "") or record.source_name or record.source_id),
                    "department": str(row.get("department") or ""),
                    "position": str(row.get("position") or ""),
                })
        placements.sort(key=lambda row: (row["organization"], row["department"], row["position"]))
        if not placements:
            raise ValueError("Нет подтвержденной активной занятости для принятия решения")
        return {"snapshot_schema_version": SNAPSHOT_SCHEMA_VERSION,
                **placements[0], "placements": placements, "active_placements_count": len(placements)}

    def find(self, event_ids: list[int]) -> AccountRequirementCase | None:
        ids = set(self.db.scalars(select(AccountRequirementArrival.case_id).where(
            AccountRequirementArrival.arrival_id.in_(event_ids),
        )).all())
        if len(ids) > 1:
            raise ValueError("Выбраны разные эпизоды решений; откройте их отдельно")
        return self.db.get(AccountRequirementCase, next(iter(ids))) if ids else None

    def event_ids(self, case: AccountRequirementCase) -> list[int]:
        return list(self.db.scalars(select(AccountRequirementArrival.arrival_id).where(
            AccountRequirementArrival.case_id == case.id,
        ).order_by(AccountRequirementArrival.arrival_id)).all())

    def current_event_ids(self, case: AccountRequirementCase) -> list[int]:
        return [event.id for event in self._arrivals(case)
                if event.status == "pending" and event.ended_at is None]

    def _event(self, case, *, previous_state, previous_decision, actor, comment=""):
        snapshot = case.decision_snapshot_json or case.initial_snapshot_json
        self.db.add(AccountRequirementDecisionEvent(
            case_id=case.id, previous_state=previous_state, previous_decision=previous_decision,
            new_state=case.state, new_decision=case.decision, actor=actor, comment=comment,
            snapshot_json=snapshot, snapshot_hash=snapshot_hash(snapshot),
        ))
        self.db.add(AuditLog(actor=actor, action="account_requirement_decision", target=str(case.id),
                             result=case.state, details=canonical_json({
                                 "previous_state": previous_state, "decision": case.decision,
                                 "snapshot_hash": snapshot_hash(snapshot),
                             })))

    def observe(self, raw_event_ids: str, account_state: dict) -> AccountRequirementCase | None:
        self.db.expire_all()
        self.assert_import_idle()
        context = EmployeeArrivalService(self.db).registration_context(raw_event_ids)
        from app.services.account_requirement_pre_registration import PreRegistrationRequirementService
        pre_case = PreRegistrationRequirementService(self.db).attach(context, account_state)
        if pre_case:
            return pre_case
        case = self.find(context["event_ids"])
        if account_state.get("errors"):
            return case  # An unavailable directory is not proof of missing accounts.
        if account_state.get("has_candidates"):
            if case and case.state in {"pending", "clarification"}:
                self.cancel(case, "Обнаружена существующая учетная запись", automatic=True)
            return None
        if case:
            for event_id in set(context["event_ids"]) - set(self.event_ids(case)):
                self.db.add(AccountRequirementArrival(arrival_id=event_id, case_id=case.id))
            self.db.commit()
            return case
        snapshot = canonical_json(self.snapshot(str(context["worker_key"])))
        case = AccountRequirementCase(worker_key=str(context["worker_key"]),
            employment_arrival_id=min(context["event_ids"]), fio=str(context["fio"]),
            initial_snapshot_json=snapshot, state="pending")
        self.db.add(case)
        self.db.flush()
        for event_id in context["event_ids"]:
            self.db.add(AccountRequirementArrival(arrival_id=event_id, case_id=case.id))
        self._event(case, previous_state="", previous_decision=None, actor="system")
        self.db.commit()
        return case

    def cancel(self, case: AccountRequirementCase, reason: str, actor: str = "system", *, automatic: bool = False) -> None:
        if case.state == "cancelled":
            return
        previous = case.state
        if automatic and case.decision is None:
            from app.models_account_requirement import AccountRequirementPreRegistration
            for waiting in self.db.scalars(select(AccountRequirementPreRegistration).where(
                AccountRequirementPreRegistration.status == "waiting",
                AccountRequirementPreRegistration.merge_case_id == case.id,
            )):
                waiting.merge_cancelled_automatically = True
        case.state = "cancelled"
        self._event(case, previous_state=previous, previous_decision=case.decision, actor=actor, comment=reason)
        self.db.commit()

    def decide(self, case_id: int, *, action: str, actor: str, is_admin: bool = False,
               comment: str = "", expected_revision: int, confirmed: bool = False) -> AccountRequirementCase:
        self.db.expire_all()
        self.assert_import_idle()
        case = self.db.get(AccountRequirementCase, case_id)
        if case is None or case.state == "cancelled":
            raise ValueError("Случай отсутствует или отменен")
        if case.revision != expected_revision:
            raise ValueError("Решение уже изменено. Обновите страницу")
        if case.decided_by and case.decided_by != actor and not is_admin:
            raise PermissionError("Исправлять чужие решения может только администратор")
        if action == "cancelled":
            if not is_admin:
                raise PermissionError("Отменять случаи может только администратор")
            if not comment.strip():
                raise ValueError("Укажите причину отмены")
            for event in self._arrivals(case):
                if event.status == "pending":
                    event.status = "cancelled"
                    event.decision_by, event.decided_at = actor, utcnow()
                    event.decision_details = "Отменен ошибочный случай необходимости УЗ"
            self.cancel(case, comment[:4000], actor=actor)
            return case
        if action not in {"required", "not_required", "clarification"}:
            raise ValueError("Неизвестное решение")
        if action == "not_required" and not confirmed:
            raise ValueError("Подтвердите, что работнику не нужна корпоративная учетная запись")
        if len(comment) > 4000:
            raise ValueError("Комментарий слишком длинный (не более 4000 символов)")
        previous_state, previous_decision = case.state, case.decision
        if action == "clarification" and case.state == "decided":
            raise ValueError("Исправьте финальное решение, не переводя его в уточнение")
        if case.state != "decided":
            raw_ids = ",".join(str(event.id) for event in self._arrivals(case)
                               if event.status == "pending" and event.ended_at is None)
            EmployeeArrivalService(self.db).registration_context(raw_ids)
            snapshot = canonical_json(self.snapshot(case.worker_key))
        else:
            # A correction fixes the label for the SAME frozen HR state.
            snapshot = case.decision_snapshot_json
        case.comment = comment.strip()
        if action == "clarification":
            case.state = "clarification"
            case.clarification_required = True
        else:
            case.state = "decided"
            case.decision = action
            if case.decision_snapshot_json is None:
                case.decision_snapshot_json = snapshot
            case.decided_by, case.decided_at = actor, utcnow()
            if case.decision_source != "pre_registration":
                case.decision_source = "manager_clarification" if case.clarification_required else "operator"
            if action == "not_required":
                for event in self._arrivals(case):
                    if event.status == "pending":
                        event.status = "not_required"
                        event.decided_at, event.decision_by = case.decided_at, actor
                        event.decision_details = "Учетная запись не требуется (решение оператора)"
            elif previous_decision == "not_required":
                for event in self._arrivals(case):
                    if event.status == "not_required" and event.ended_at is None:
                        event.status = "pending"
        self._event(case, previous_state=previous_state, previous_decision=previous_decision,
                    actor=actor, comment=case.comment)
        self.db.commit()
        return case

    def _arrivals(self, case):
        return self.db.scalars(select(HREmploymentArrivalEvent).where(
            HREmploymentArrivalEvent.id.in_(self.event_ids(case)),
        )).all()

    def record_external_result(self, case_id: int, credentials=None, *, failed=False):
        case = self.db.get(AccountRequirementCase, case_id)
        if case is None:
            return
        if credentials is not None:
            case.created_account = bool(not credentials.dry_run and (credentials.ad_created or credentials.zimbra_created))
            case.external_action_status = "dry_run" if credentials.dry_run else credentials.status
            case.provisioning_operation_id = credentials.operation_id
        elif failed:
            case.external_action_status = "failed"
        self.db.commit()

    def cancel_stale(self):
        for case in self.db.scalars(select(AccountRequirementCase).where(
            AccountRequirementCase.state.in_(["pending", "clarification"]),
        )).all():
            events = self._arrivals(case)
            if not any(row.ended_at is None and row.status == "pending" for row in events):
                self.cancel(case, "Кадровый эпизод завершен или приняты существующие учетки")


def build_account_requirement_dataset(db: Session) -> dict:
    """Frozen decision/first-confirmed-hire features; no identity or outcome in X."""
    from app.models_account_requirement import AccountRequirementPreRegistration
    pre_cases = {row.case_id: row for row in db.scalars(select(AccountRequirementPreRegistration).where(
        AccountRequirementPreRegistration.status == "matched",
    ))}
    pre_case_ids = set(pre_cases)
    def timestamp(value):
        if value is None:
            return None
        return (value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value.astimezone(timezone.utc)).isoformat()
    rows = []
    for case in db.scalars(select(AccountRequirementCase).where(
        AccountRequirementCase.state == "decided",
        AccountRequirementCase.decision.in_(["required", "not_required"]),
    ).order_by(AccountRequirementCase.id)).all():
        service = AccountRequirementService(db)
        events = service._arrivals(case)
        if not events or (case.id not in pre_case_ids and any(event.status == "accounts_confirmed" for event in events)):
            continue
        snapshot = json.loads(case.decision_snapshot_json or "null")
        if not isinstance(snapshot, dict) or snapshot.get("snapshot_schema_version") != SNAPSHOT_SCHEMA_VERSION:
            continue
        placements = snapshot.get("placements")
        if not isinstance(placements, list) or not placements:
            continue
        # Export allowlist also protects against accidental future snapshot expansion.
        features = {key: snapshot.get(key, "") for key in ("organization", "department", "position")}
        features["placements"] = [{key: str(row.get(key) or "") for key in ("organization", "department", "position")}
                                  for row in placements if isinstance(row, dict)]
        features["active_placements_count"] = len(features["placements"])
        rows.append({"case_id": case.id, "snapshot_schema_version": SNAPSHOT_SCHEMA_VERSION,
                     "snapshot_hash": snapshot_hash(case.decision_snapshot_json),
                     "feature_source": "first_confirmed_hire" if case.id in pre_case_ids else "decision_time",
                     "decision_at": timestamp(case.decided_at),
                     "original_decision_at": timestamp(pre_cases[case.id].decided_at) if case.id in pre_case_ids else timestamp(case.decided_at),
                     "hr_confirmed_at": timestamp(case.hr_confirmed_at),
                     "X": features, "y": int(case.decision == "required")})
    content = {"dataset_schema_version": 2, "snapshot_schema_version": SNAPSHOT_SCHEMA_VERSION,
               "records_count": len(rows), "records": rows}
    return {**content, "dataset_sha256": snapshot_hash(canonical_json(content))}
