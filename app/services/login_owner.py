"""Read-only collision guidance. Never authorizes account deletion or restoration."""
from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import EmailLoginMapping, HRSourceRecord
from app.models_account_requirement import AccountRequirementPreRegistration
from app.services.ad import ActiveDirectoryService
from app.services.zimbra import ZimbraService
from app.services.onec_import_recovery import running_import
from app.services.personnel_structure import PersonnelStructureService


def normalized(value: object) -> str:
    return str(value or "").strip().lower().strip("{}")


class LoginOwnerService:
    def __init__(self, db: Session, settings):
        self.db, self.settings = db, settings

    def classify(self, *, source: str, stable_id: str, addresses=()) -> dict:
        """Use the actual account identity, not a generated login or FIO guess."""
        unknown = {"state": "unknown", "fio": "", "message": "Владелец не установлен. Проверьте сопоставление."}
        if running_import(self.db):
            return {**unknown, "message": "Кадровая выгрузка обновляется. Повторите проверку позже."}
        if not stable_id:
            return unknown
        stable_id = normalized(stable_id)
        field = "ad_object_guid" if source == "ad" else "zimbra_id"
        mappings = list(self.db.scalars(select(EmailLoginMapping)).all())
        keys = {row.worker_key for row in mappings
                if normalized(getattr(row, field)) == stable_id and row.worker_key}
        addresses = {normalized(value) for value in addresses if value}
        records = list(self.db.scalars(select(HRSourceRecord)).all())
        # Exact addresses may identify the owner for display. Conflicting ID/address
        # evidence is deliberately ambiguous; it must not select one side silently.
        keys.update(row.worker_key for row in records
                    if row.worker_key and normalized(row.corporate_email) in addresses)
        waiting = list(self.db.scalars(select(AccountRequirementPreRegistration).where(
            AccountRequirementPreRegistration.status == "waiting",
        )).all())
        pending = [row for row in waiting if normalized(getattr(row, field)) == stable_id]
        if len(keys) > 1 or len(pending) > 1:
            return {**unknown, "state": "ambiguous", "message": "Неоднозначный владелец. Проверьте сопоставление."}
        if not keys:
            if pending:
                return {"state": "waiting_hr", "fio": pending[0].fio,
                        "message": "УЗ создана до оформления. Ожидает кадрового подтверждения."}
            return unknown
        key = next(iter(keys))
        worker = PersonnelStructureService(self.db).worker_state(key)
        if pending:
            # The waiting pre-registration is not yet linked to an HR identity.
            return {**unknown, "state": "ambiguous", "message": "Есть создание до оформления. Проверьте сопоставление."}
        if worker.active_anywhere:
            return {"state": "active", "fio": worker.fio,
                    "message": "Действующий работник. Для другого человека выберите новый логин."}
        return {"state": "inactive", "fio": worker.fio,
                "message": "Активная занятость не найдена. Это не разрешение на удаление УЗ."}

    def inspect(self, login: str) -> list[dict]:
        result = []
        if self.settings.ad_check_enabled:
            try:
                user = ActiveDirectoryService(self.settings).get_user(login)
                if user:
                    result.append({"source": "Active Directory", **self.classify(
                        source="ad", stable_id=user.object_guid, addresses=[user.email])})
            except Exception:
                result.append({"source": "Active Directory", "state": "error", "fio": "",
                               "message": "Не удалось проверить владельца. Повторите проверку."})
        if self.settings.zimbra_check_enabled:
            try:
                domains = self.settings.zimbra_domains or [self.settings.zimbra_primary_domain]
                accounts = ZimbraService(self.settings).accounts_by_addresses(
                    [f"{login}@{domain}" for domain in domains if domain])
                unique = {normalized(account.zimbra_id): account for account in accounts.values()}
                for account in unique.values():
                    result.append({"source": "Zimbra", **self.classify(
                        source="zimbra", stable_id=account.zimbra_id, addresses=account.addresses)})
            except Exception:
                result.append({"source": "Zimbra", "state": "error", "fio": "",
                               "message": "Не удалось проверить владельца. Повторите проверку."})
        return result or [{"source": "", "state": "unknown", "fio": "",
                           "message": "Владелец не установлен. Проверьте сопоставление."}]
