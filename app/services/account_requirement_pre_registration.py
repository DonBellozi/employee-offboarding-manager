from __future__ import annotations

from datetime import timezone

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import AuditLog, EmailLoginMapping, HRSourceRecord, ProvisioningOperation
from app.models_account_requirement import (
    AccountRequirementArrival, AccountRequirementCase, AccountRequirementPreRegistration, utcnow,
)
from app.models_employee_arrivals import HREmploymentArrivalEvent
from app.services.account_requirement import AccountRequirementService, canonical_json, snapshot_hash
from app.services.worker_identity import normalize_fio, normalize_email


def aware(value):
    return value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value.astimezone(timezone.utc)


class PreRegistrationRequirementService:
    def __init__(self, db: Session):
        self.db = db

    def begin(self, operation: ProvisioningOperation) -> AccountRequirementPreRegistration:
        """Called after validation/preflight, in the same transaction as the operation."""
        existing = self.db.scalar(select(AccountRequirementPreRegistration).where(
            AccountRequirementPreRegistration.provisioning_operation_id == operation.id,
        ))
        if existing:
            return existing
        snapshot = canonical_json({"snapshot_schema_version": 1, "organization": "", "department": "",
                                   "position": "", "placements": [], "active_placements_count": 0})
        row = AccountRequirementPreRegistration(
            provisioning_operation_id=operation.id,
            fio=" ".join(part for part in (operation.last_name, operation.first_name, operation.middle_name) if part),
            decided_by=operation.operator_username, decided_at=operation.created_at,
            initial_snapshot_json=snapshot,
        )
        self.db.add(row)
        self.db.flush()
        self.db.add(AuditLog(actor=row.decided_by, action="account_requirement_pre_registration",
                             target=str(row.id), result="required",
                             details=canonical_json({"operation_id": operation.id, "snapshot_hash": snapshot_hash(snapshot)})))
        return row

    def pin_created_accounts(self, row, operation, ad, zimbra) -> None:
        """Read-only identity capture; directory errors do not undo provisioning."""
        if operation.ad_created:
            try:
                user = ad.get_user(operation.login)
                if user and isinstance(user.object_guid, str):
                    row.ad_object_guid = user.object_guid
            except Exception:
                pass
        if operation.zimbra_created:
            try:
                mailbox = zimbra.account_by_address(operation.corporate_email)
                if mailbox and isinstance(mailbox.zimbra_id, str):
                    row.zimbra_id = mailbox.zimbra_id
            except Exception:
                pass
        if not row.ad_object_guid and not row.zimbra_id:
            row.match_note = "Ожидает кадрового подтверждения и проверенного сопоставления учеток"

    def _evidence(self, row, operation, context, state) -> bool:
        mappings = self.db.scalars(select(EmailLoginMapping).where(
            EmailLoginMapping.worker_key == context["worker_key"],
        )).all()
        other_mappings = self.db.scalars(select(EmailLoginMapping).where(
            EmailLoginMapping.worker_key != context["worker_key"],
        )).all()
        def conflicts(guid, zid):
            return any((guid and normalize_email(other.ad_object_guid) == normalize_email(guid))
                       or (zid and normalize_email(other.zimbra_id) == normalize_email(zid)) for other in other_mappings)
        if conflicts(row.ad_object_guid, row.zimbra_id):
            return False
        # Explicit existing operator mapping is authoritative, including a name change.
        for mapping in mappings:
            if (mapping.last_verified_at and aware(mapping.last_verified_at) >= aware(row.decided_at)
                    and ((row.ad_object_guid and row.zimbra_id)
                         or (normalize_email(mapping.ad_login) == normalize_email(operation.login)
                             and normalize_email(mapping.zimbra_email) == normalize_email(operation.corporate_email)))
                    and mapping.ad_object_guid and mapping.zimbra_id
                    and (not row.ad_object_guid or normalize_email(mapping.ad_object_guid) == normalize_email(row.ad_object_guid))
                    and (not row.zimbra_id or normalize_email(mapping.zimbra_id) == normalize_email(row.zimbra_id))
                    and not conflicts(mapping.ad_object_guid, mapping.zimbra_id)):
                return True
        if not state or state.get("errors") or normalize_fio(context["fio"]) != normalize_fio(row.fio):
            return False
        ad_rows, mail_rows = state.get("ad_candidates", []), state.get("zimbra_candidates", [])
        # Automatic binding needs pinned IDs, unambiguous discovery AND unique HR evidence.
        if not (row.ad_object_guid or row.zimbra_id) or len(ad_rows) > 1 or len(mail_rows) > 1:
            return False
        if row.ad_object_guid and not any(normalize_email(item.get("object_guid")) == normalize_email(row.ad_object_guid) for item in ad_rows):
            return False
        if row.zimbra_id and not any(normalize_email(item.get("zimbra_id")) == normalize_email(row.zimbra_id) for item in mail_rows):
            return False
        addresses = {normalize_email(operation.corporate_email)}
        for mailbox in mail_rows:
            addresses.update(normalize_email(value) for value in mailbox.get("addresses", []))
        keys = {record.worker_key for record in self.db.scalars(select(HRSourceRecord).where(
            HRSourceRecord.is_present.is_(True),
        )) if normalize_fio(record.fio) == normalize_fio(row.fio)
                and normalize_email(record.corporate_email) in addresses}
        if keys != {context["worker_key"]}:
            return False
        # A pinned account already belonging to another person cannot be borrowed.
        return True

    def attach(self, context: dict, state: dict | None = None) -> AccountRequirementCase | None:
        service = AccountRequirementService(self.db)
        service.assert_import_idle()
        existing = service.find(context["event_ids"])
        if existing and self.db.scalar(select(AccountRequirementPreRegistration.id).where(
            AccountRequirementPreRegistration.case_id == existing.id,
            AccountRequirementPreRegistration.status == "matched",
        )):
            return existing
        candidates = []
        for row in self.db.scalars(select(AccountRequirementPreRegistration).where(
            AccountRequirementPreRegistration.status == "waiting",
        )).all():
            operation = self.db.get(ProvisioningOperation, row.provisioning_operation_id)
            if not operation or not (operation.ad_created or operation.zimbra_created):
                continue
            if aware(row.decided_at) > min(aware(event.first_seen_at) for event in context["events"]):
                continue
            earlier = self.db.scalar(select(HREmploymentArrivalEvent.id).where(
                HREmploymentArrivalEvent.worker_key == context["worker_key"],
                HREmploymentArrivalEvent.first_seen_at >= row.decided_at,
                HREmploymentArrivalEvent.first_seen_at < min(event.first_seen_at for event in context["events"]),
            ).limit(1))
            if earlier:  # Never attach an old decision to a later rehire/transfer.
                continue
            if self._evidence(row, operation, context, state):
                candidates.append(row)
            elif normalize_fio(context["fio"]) == normalize_fio(row.fio):
                row.match_note = "Ожидает проверенного сопоставления работника с созданными учетками"
        if not candidates:
            if self.db.dirty:
                self.db.commit()
            return None
        can_merge_cancelled = bool(existing and existing.state == "cancelled" and existing.decision is None
                                   and len(candidates) == 1 and candidates[0].merge_case_id == existing.id
                                   and candidates[0].merge_cancelled_automatically)
        if len(candidates) > 1 or (existing and existing.state != "pending" and not can_merge_cancelled):
            for row in candidates:
                row.match_note = "Неоднозначное сопоставление: уже есть другое решение или несколько регистраций"
            self.db.commit()
            return None
        row = candidates[0]
        # The first complete HR confirmation is frozen, never replaced after a transfer.
        snapshot = service.snapshot(str(context["worker_key"]))
        if not all(all(str(item.get(key) or "").strip() for key in ("organization", "department", "position"))
                   for item in snapshot["placements"]):
            row.match_note = "Ожидает полных кадровых сведений: организация, подразделение и должность"
            if existing and existing.state == "pending":
                row.merge_case_id = existing.id
            self.db.commit()
            return None
        case = existing or AccountRequirementCase(
            worker_key=str(context["worker_key"]), employment_arrival_id=min(context["event_ids"]),
            fio=str(context["fio"]), initial_snapshot_json=row.initial_snapshot_json,
        )
        previous_state, previous_decision = case.state or "", case.decision
        case.state, case.decision, case.decision_source = "decided", "required", "pre_registration"
        case.decided_by, case.decided_at = row.decided_by, row.decided_at
        case.decision_snapshot_json = canonical_json(snapshot)
        case.hr_confirmed_at = utcnow()
        operation = self.db.get(ProvisioningOperation, row.provisioning_operation_id)
        case.created_account = bool(operation.ad_created or operation.zimbra_created)
        case.external_action_status = operation.status.value
        case.provisioning_operation_id = operation.id
        self.db.add(case)
        self.db.flush()
        for event_id in set(context["event_ids"]) - set(service.event_ids(case)):
            self.db.add(AccountRequirementArrival(arrival_id=event_id, case_id=case.id))
        row.status, row.case_id, row.match_note = "matched", case.id, "Кадровый прием подтвержден"
        service._event(case, previous_state=previous_state, previous_decision=previous_decision,
                       actor=row.decided_by, comment="Решение принято при создании до выгрузки; кадровые сведения подтверждены позднее")
        self.db.commit()
        return case

    def reconcile_confirmed(self) -> None:
        """Retry incomplete HR features after accounts were accepted by an operator."""
        from collections import defaultdict
        waiting = self.db.scalars(select(AccountRequirementPreRegistration).where(
            AccountRequirementPreRegistration.status == "waiting",
        )).all()
        if not waiting:
            return
        AccountRequirementService(self.db).assert_import_idle()
        mapping_keys = {mapping.worker_key for mapping in self.db.scalars(select(EmailLoginMapping))
                        if any((row.ad_object_guid and normalize_email(row.ad_object_guid) == normalize_email(mapping.ad_object_guid))
                               or (row.zimbra_id and normalize_email(row.zimbra_id) == normalize_email(mapping.zimbra_id))
                               for row in waiting)}
        # If initial identity capture failed, an explicit verified mapping may still bind.
        operations = [self.db.get(ProvisioningOperation, row.provisioning_operation_id) for row in waiting]
        mapping_keys.update(mapping.worker_key for mapping in self.db.scalars(select(EmailLoginMapping))
                            if any(operation and normalize_email(operation.login) == normalize_email(mapping.ad_login)
                                   and normalize_email(operation.corporate_email) == normalize_email(mapping.zimbra_email)
                                   for operation in operations))
        grouped = defaultdict(list)
        for event in self.db.scalars(select(HREmploymentArrivalEvent).where(
            HREmploymentArrivalEvent.worker_key.in_(mapping_keys),
            HREmploymentArrivalEvent.status.in_(["accounts_confirmed", "registered"]),
            HREmploymentArrivalEvent.ended_at.is_(None),
        )).all():
            grouped[event.worker_key].append(event)
        for key, events in grouped.items():
            records = self.db.scalars(select(HRSourceRecord).where(
                HRSourceRecord.worker_key == key, HRSourceRecord.is_present.is_(True),
                HRSourceRecord.source_id.in_([event.source_id for event in events]),
            )).all()
            if len(records) != len(events):
                continue
            try:
                self.attach({"worker_key": key, "fio": events[0].fio, "events": events,
                             "records": records, "event_ids": [event.id for event in events]})
            except ValueError:
                # A transfer/ended source/incomplete import cannot change the earlier label.
                self.db.rollback()

    def cancel(self, row_id: int, *, actor: str, comment: str, revision: int):
        row = self.db.get(AccountRequirementPreRegistration, row_id)
        if row is None or row.status != "waiting" or row.revision != revision:
            raise ValueError("Запись уже изменена; обновите страницу")
        if not comment.strip() or len(comment) > 4000:
            raise ValueError("Укажите причину отмены (до 4000 символов)")
        row.status, row.match_note = "cancelled", comment.strip()
        self.db.add(AuditLog(actor=actor, action="account_requirement_pre_registration_cancelled",
                             target=str(row.id), result="cancelled",
                             details=canonical_json({"comment": row.match_note, "operation_id": row.provisioning_operation_id})))
        self.db.commit()
