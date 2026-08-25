from __future__ import annotations

import json
import unicodedata
from datetime import datetime, timezone
from urllib.parse import urlencode

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import Settings
from app.models import AuditLog, EmailLoginMapping, HRSourceRecord
from app.services.ad import (
    ADDirectoryUser,
    ActiveDirectoryService,
    wait_for_reactivated_user,
)
from app.services.employee_arrivals import EmployeeArrivalService
from app.services.zimbra import ZimbraAccountIdentity, ZimbraService


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def normalize_email(value: object) -> str:
    return " ".join(str(value or "").replace("\u00a0", " ").split()).casefold()


def normalize_fio(value: object) -> str:
    text = unicodedata.normalize("NFKC", str(value or ""))
    return " ".join(text.replace("\u00a0", " ").split()).casefold().replace(
        "ё",
        "е",
    )


class EmployeeArrivalAccountService:
    """Проверка и принятие существующих учёток при новом кадровом эпизоде."""

    def __init__(self, settings: Settings, db: Session):
        self.settings = settings
        self.db = db

    @staticmethod
    def _ad_key(user: ADDirectoryUser) -> str:
        return normalize_email(user.object_guid) or normalize_email(
            user.username
        )

    @staticmethod
    def _zimbra_key(account: ZimbraAccountIdentity) -> str:
        return normalize_email(account.zimbra_id) or normalize_email(
            account.primary_email
        )

    @staticmethod
    def _ad_payload(user: ADDirectoryUser) -> dict[str, object]:
        status = (
            "disabled"
            if not user.is_enabled
            else "expired"
            if user.is_expired
            else "active"
        )
        return {
            "username": user.username,
            "display_name": user.display_name,
            "email": user.email,
            "object_guid": user.object_guid,
            "is_enabled": bool(user.is_enabled),
            "is_expired": bool(user.is_expired),
            "status": status,
        }

    @staticmethod
    def _zimbra_payload(
        account: ZimbraAccountIdentity,
    ) -> dict[str, object]:
        return {
            "zimbra_id": account.zimbra_id,
            "primary_email": account.primary_email,
            "login": account.login,
            "addresses": list(account.addresses),
            "status": normalize_email(account.account_status) or "unknown",
        }

    def inspect(self, raw_event_ids: str) -> dict[str, object]:
        context = EmployeeArrivalService(self.db).registration_context(
            raw_event_ids
        )
        mappings = list(
            self.db.scalars(
                select(EmailLoginMapping).where(
                    EmailLoginMapping.worker_key == context["worker_key"]
                )
            ).all()
        )
        records: list[HRSourceRecord] = list(context["records"])
        ad_candidates: dict[str, ADDirectoryUser] = {}
        zimbra_candidates: dict[str, ZimbraAccountIdentity] = {}
        errors: list[str] = []

        def add_ad(user: ADDirectoryUser | None) -> None:
            if user is None:
                return
            key = self._ad_key(user)
            if key:
                ad_candidates[key] = user

        def add_zimbra(account: ZimbraAccountIdentity | None) -> None:
            if account is None:
                return
            key = self._zimbra_key(account)
            if key:
                zimbra_candidates[key] = account

        if self.settings.ad_check_enabled:
            try:
                ad = ActiveDirectoryService(self.settings)
                guids = {
                    normalize_email(mapping.ad_object_guid)
                    for mapping in mappings
                    if normalize_email(mapping.ad_object_guid)
                }
                logins = {
                    normalize_email(value)
                    for value in [
                        *(mapping.ad_login for mapping in mappings),
                        *(record.login for record in records),
                        *(
                            email.rsplit("@", 1)[0]
                            for email in context["corporate_emails"]
                            if "@" in email
                        ),
                    ]
                    if normalize_email(value)
                }
                emails = {
                    normalize_email(value)
                    for value in [
                        *context["corporate_emails"],
                        *(mapping.source_email for mapping in mappings),
                    ]
                    if normalize_email(value)
                }
                for guid in sorted(guids):
                    add_ad(ad.get_user_by_object_guid(guid))
                for login in sorted(logins):
                    add_ad(ad.get_user(login))
                for email in sorted(emails):
                    for user in ad.users_by_email(email, limit=10):
                        add_ad(user)
                fio_key = normalize_fio(context["fio"])
                if fio_key:
                    for user in ad.search_users(str(context["fio"]), limit=20):
                        if normalize_fio(user.display_name) == fio_key:
                            add_ad(user)
            except Exception as exc:
                errors.append(f"AD: {exc}")
        else:
            errors.append("Проверка AD отключена в настройках")

        if self.settings.zimbra_check_enabled:
            try:
                zimbra = ZimbraService(self.settings)
                ids = [
                    mapping.zimbra_id
                    for mapping in mappings
                    if str(mapping.zimbra_id or "").strip()
                ]
                for account in zimbra.accounts_by_ids(ids).values():
                    add_zimbra(account)
                known_logins = {
                    normalize_email(value).split("@", 1)[0]
                    for value in [
                        *context["logins"],
                        *(mapping.ad_login for mapping in mappings),
                        *(mapping.zimbra_email for mapping in mappings),
                    ]
                    if normalize_email(value)
                }
                known_domains = {
                    normalize_email(value).lstrip("@")
                    for value in [
                        *(
                            getattr(
                                self.settings,
                                "zimbra_domains",
                                (),
                            )
                            or ()
                        ),
                        getattr(
                            self.settings,
                            "zimbra_primary_domain",
                            "",
                        ),
                    ]
                    if normalize_email(value).lstrip("@")
                }
                addresses = list(
                    dict.fromkeys(
                        normalize_email(value)
                        for value in [
                            *context["corporate_emails"],
                            *(mapping.source_email for mapping in mappings),
                            *(mapping.zimbra_email for mapping in mappings),
                            *(
                                f"{login}@{domain}"
                                for login in sorted(known_logins)
                                for domain in sorted(known_domains)
                            ),
                        ]
                        if normalize_email(value)
                    )
                )
                for account in zimbra.accounts_by_addresses(
                    addresses
                ).values():
                    add_zimbra(account)
            except Exception as exc:
                errors.append(f"Zimbra: {exc}")
        else:
            errors.append("Проверка Zimbra отключена в настройках")

        ad_rows = sorted(
            (self._ad_payload(user) for user in ad_candidates.values()),
            key=lambda item: (
                not bool(item["is_enabled"]),
                str(item["username"]).casefold(),
            ),
        )
        zimbra_rows = sorted(
            (
                self._zimbra_payload(account)
                for account in zimbra_candidates.values()
            ),
            key=lambda item: (
                str(item["status"]) != "active",
                str(item["primary_email"]).casefold(),
            ),
        )
        selected_ad = ad_rows[0] if len(ad_rows) == 1 else None
        selected_zimbra = (
            zimbra_rows[0] if len(zimbra_rows) == 1 else None
        )
        mail_only = bool(selected_zimbra is not None and not ad_rows)
        can_create_missing_ad = bool(
            mail_only
            and self.settings.ad_check_enabled
            and self.settings.zimbra_check_enabled
        )
        unique_pair = selected_ad is not None and selected_zimbra is not None
        pair_active = bool(
            unique_pair
            and selected_ad["is_enabled"]
            and not selected_ad["is_expired"]
            and selected_zimbra["status"] == "active"
        )
        pair_restorable = bool(
            unique_pair
            and (
                not selected_ad["is_enabled"]
                or selected_ad["is_expired"]
                or selected_zimbra["status"] == "closed"
            )
            and selected_zimbra["status"] in {"active", "closed"}
        )
        return {
            "event_ids_value": context["event_ids_value"],
            "fio": context["fio"],
            "worker_key": context["worker_key"],
            "corporate_emails": context["corporate_emails"],
            "hr_logins": context["logins"],
            "ad_candidates": ad_rows,
            "zimbra_candidates": zimbra_rows,
            "errors": errors,
            "has_candidates": bool(ad_rows or zimbra_rows),
            "unique_pair": unique_pair,
            "pair_active": pair_active,
            "pair_restorable": pair_restorable,
            "mail_only": mail_only,
            "create_missing_ad_url": (
                "/employees/arrivals/accounts/create-missing-ad?"
                + urlencode(
                    {
                        "arrival_event_ids": context["event_ids_value"],
                        "zimbra_email": selected_zimbra["primary_email"],
                    }
                )
                if can_create_missing_ad
                else ""
            ),
            "create_missing_ad_label": (
                "Восстановить почту и создать AD"
                if mail_only and selected_zimbra["status"] == "closed"
                else "Создать недостающую AD"
            ),
            "suggested_ad_login": (
                str(selected_ad["username"])
                if selected_ad is not None
                else next(iter(context["logins"]), "")
            ),
            "suggested_zimbra_email": (
                str(selected_zimbra["primary_email"])
                if selected_zimbra is not None
                else next(iter(context["corporate_emails"]), "")
            ),
        }

    def _zimbra_conflict(
        self,
        *,
        worker_key: str,
        zimbra: ZimbraAccountIdentity,
    ) -> EmailLoginMapping | None:
        stable_id = normalize_email(zimbra.zimbra_id)
        for mapping in self.db.scalars(
            select(EmailLoginMapping).where(
                EmailLoginMapping.worker_key != worker_key
            )
        ).all():
            if stable_id and normalize_email(mapping.zimbra_id) == stable_id:
                return mapping
        return None

    @staticmethod
    def _record_for_mailbox(
        context: dict[str, object],
        zimbra: ZimbraAccountIdentity,
    ) -> HRSourceRecord:
        addresses = {
            normalize_email(value)
            for value in (
                zimbra.primary_email,
                *zimbra.addresses,
            )
            if normalize_email(value)
        }
        records = [
            record
            for record in context["records"]
            if normalize_email(record.corporate_email) in addresses
        ]
        if not records:
            raise ValueError(
                "Выбранный почтовый ящик не относится к текущей кадровой "
                "записи работника"
            )
        if len(records) > 1:
            raise ValueError(
                "Один почтовый ящик найден сразу в нескольких организациях. "
                "Сначала уточните кадровое сопоставление."
            )
        return records[0]

    def prepare_missing_ad(
        self,
        *,
        raw_event_ids: str,
        zimbra_email: str,
    ) -> dict[str, object]:
        """Проверить создание только AD для найденного почтового ящика."""
        from app.services.provisioning import ProvisioningService

        if not self.settings.ad_check_enabled:
            raise ValueError("Проверка и создание AD отключены в настройках")
        if not self.settings.zimbra_check_enabled:
            raise ValueError("Проверка Zimbra отключена в настройках")

        context = EmployeeArrivalService(self.db).registration_context(
            raw_event_ids
        )
        normalized_email = normalize_email(zimbra_email)
        if not normalized_email or "@" not in normalized_email:
            raise ValueError("Укажите существующий корпоративный адрес")

        zimbra = ZimbraService(self.settings).account_by_address(
            normalized_email
        )
        if zimbra is None:
            raise ValueError(
                f"Zimbra: ящик {normalized_email} больше не найден"
            )
        if not zimbra.zimbra_id:
            raise ValueError("Zimbra не вернула zimbraId почтового ящика")
        status = normalize_email(zimbra.account_status)
        if status not in {"active", "closed"}:
            raise ValueError(
                "Использовать можно только активный или закрытый почтовый "
                f"ящик; текущий статус — {status or 'не указан'}"
            )
        if self._zimbra_conflict(
            worker_key=str(context["worker_key"]),
            zimbra=zimbra,
        ) is not None:
            raise ValueError(
                "Выбранный почтовый ящик уже сопоставлен с другим работником"
            )

        record = self._record_for_mailbox(context, zimbra)
        preflight = ProvisioningService(
            self.settings
        ).prepare_ad_for_existing_mailbox(
            self.db,
            record.id,
        )
        return {
            "context": context,
            "record": record,
            "mailbox": zimbra,
            "mailbox_status": status,
            "preflight": preflight,
        }

    def create_missing_ad(
        self,
        *,
        raw_event_ids: str,
        zimbra_email: str,
        actor: str,
        confirm_name_candidates: bool = False,
    ) -> dict[str, object]:
        """Открыть старую почту, создать недостающую AD и закрыть событие."""
        from app.services.provisioning import ProvisioningService

        prepared = self.prepare_missing_ad(
            raw_event_ids=raw_event_ids,
            zimbra_email=zimbra_email,
        )
        preflight = prepared["preflight"]
        if not preflight.can_create:
            raise RuntimeError(preflight.block_reason)
        if preflight.name_candidates and not confirm_name_candidates:
            raise RuntimeError(
                "В AD найдены возможные совпадения по ФИО. "
                "Выберите существующую учетную запись либо подтвердите "
                "создание новой."
            )

        mailbox = prepared["mailbox"]
        mailbox_restored = False
        if (
            prepared["mailbox_status"] == "closed"
            and not self.settings.dry_run
        ):
            zimbra_service = ZimbraService(self.settings)
            zimbra_service.open_account(mailbox.primary_email)
            mailbox = zimbra_service.account_by_address(
                mailbox.primary_email
            )
            if (
                mailbox is None
                or normalize_email(mailbox.account_status) != "active"
            ):
                raise RuntimeError(
                    "Zimbra не подтвердила открытие почтового ящика"
                )
            mailbox_restored = True
            self.db.add(
                AuditLog(
                    actor=actor,
                    action="new_employment_mailbox_restored_for_ad",
                    target=mailbox.primary_email,
                    result="success",
                    details=json.dumps(
                        {
                            "worker_key": prepared["context"]["worker_key"],
                            "event_ids": prepared["context"]["event_ids"],
                            "zimbra_id": mailbox.zimbra_id,
                        },
                        ensure_ascii=False,
                        sort_keys=True,
                    ),
                )
            )
            self.db.commit()

        credentials = ProvisioningService(
            self.settings
        ).provision_ad_for_existing_mailbox(
            self.db,
            actor,
            prepared["record"].id,
            confirm_name_candidates=confirm_name_candidates,
        )
        arrival_result = None
        arrival_error = ""
        if credentials.dry_run:
            arrival_error = (
                "DRY_RUN: кадровое уведомление осталось открытым, "
                "поскольку реальные учетные записи не изменялись."
            )
        elif not credentials.ad_created or not credentials.ad_enabled:
            arrival_error = (
                "Кадровое уведомление осталось открытым: создание или "
                "включение AD завершилось не полностью."
            )
        else:
            try:
                arrival_result = self.resolve(
                    raw_event_ids=raw_event_ids,
                    ad_login=credentials.ad_login,
                    zimbra_email=mailbox.primary_email,
                    actor=actor,
                    restore_closed=False,
                    provisioning_operation_id=credentials.operation_id,
                )
            except Exception as exc:
                self.db.rollback()
                arrival_error = (
                    "AD создана, но уведомление о возвращении осталось "
                    f"открытым: {exc}"
                )

        return {
            "credentials": credentials,
            "arrival_result": arrival_result,
            "arrival_error": arrival_error,
            "mailbox_restored": mailbox_restored,
            "mailbox_email": mailbox.primary_email,
        }

    def _conflict(
        self,
        *,
        worker_key: str,
        ad_user: ADDirectoryUser,
        zimbra: ZimbraAccountIdentity,
    ) -> EmailLoginMapping | None:
        for mapping in self.db.scalars(
            select(EmailLoginMapping).where(
                EmailLoginMapping.worker_key != worker_key
            )
        ).all():
            if (
                normalize_email(mapping.ad_object_guid)
                == normalize_email(ad_user.object_guid)
                or normalize_email(mapping.zimbra_id)
                == normalize_email(zimbra.zimbra_id)
            ):
                return mapping
        return None

    def resolve(
        self,
        *,
        raw_event_ids: str,
        ad_login: str,
        zimbra_email: str,
        actor: str,
        restore_closed: bool,
        provisioning_operation_id: int | None = None,
    ) -> dict[str, object]:
        context = EmployeeArrivalService(self.db).registration_context(
            raw_event_ids
        )
        normalized_login = normalize_email(ad_login)
        normalized_email = normalize_email(zimbra_email)
        if not normalized_login:
            raise ValueError("Укажите логин AD")
        if not normalized_email or "@" not in normalized_email:
            raise ValueError("Укажите адрес почтового ящика Zimbra")

        ad = ActiveDirectoryService(self.settings)
        zimbra_service = ZimbraService(self.settings)
        ad_user = ad.get_user(normalized_login)
        if ad_user is None:
            raise ValueError(
                f"AD: учетная запись {normalized_login} не найдена"
            )
        if not ad_user.object_guid:
            raise ValueError("AD не вернул objectGUID учетной записи")
        zimbra = zimbra_service.account_by_address(normalized_email)
        if zimbra is None:
            raise ValueError(
                f"Zimbra: ящик {normalized_email} не найден"
            )
        if not zimbra.zimbra_id:
            raise ValueError("Zimbra не вернула zimbraId почтового ящика")

        # Один кадровый сигнал может объединять возвращение сразу в несколько
        # организаций. Не записываем выбранный ящик во все организации, если
        # один из их корпоративных адресов уже ведет на другой zimbraId.
        records = list(context["records"])
        record_emails = list(
            dict.fromkeys(
                normalize_email(record.corporate_email)
                for record in records
                if normalize_email(record.corporate_email)
            )
        )
        if len(records) > 1 and record_emails:
            organization_accounts = zimbra_service.accounts_by_addresses(
                record_emails
            )
            different_accounts = {
                account.zimbra_id
                for account in organization_accounts.values()
                if account.zimbra_id and account.zimbra_id != zimbra.zimbra_id
            }
            if different_accounts:
                raise ValueError(
                    "У работника найдены разные почтовые ящики организаций. "
                    "Их нельзя заменить одним сопоставлением; проверьте "
                    "возвращение отдельно по каждой организации."
                )
        conflict = self._conflict(
            worker_key=str(context["worker_key"]),
            ad_user=ad_user,
            zimbra=zimbra,
        )
        if conflict is not None:
            raise ValueError(
                "Выбранная учетная запись AD или Zimbra уже сопоставлена "
                "с другим работником"
            )

        zimbra_status = normalize_email(zimbra.account_status)
        if restore_closed:
            if zimbra_status not in {"active", "closed"}:
                raise ValueError(
                    "Автоматически открыть можно только ящик со статусом "
                    f"closed; текущий статус — {zimbra_status or 'не указан'}"
                )
            if not ad_user.is_enabled or ad_user.is_expired:
                ad.reactivate_existing_user(ad_user.distinguished_name)
                ad_user = wait_for_reactivated_user(
                    ad,
                    object_guid=ad_user.object_guid,
                    username=ad_user.username,
                )
                if (
                    ad_user is None
                    or not ad_user.is_enabled
                    or ad_user.is_expired
                ):
                    raise RuntimeError(
                        "AD не подтвердил восстановление учетной записи"
                    )
            if zimbra_status == "closed":
                zimbra_service.open_account(zimbra.primary_email)
                zimbra = zimbra_service.account_by_address(
                    zimbra.primary_email
                )
                if (
                    zimbra is None
                    or normalize_email(zimbra.account_status) != "active"
                ):
                    raise RuntimeError(
                        "Zimbra не подтвердила открытие почтового ящика"
                    )
                zimbra_status = "active"
        else:
            if not ad_user.is_enabled:
                raise ValueError(
                    "Учетная запись AD отключена. Используйте действие "
                    "«Восстановить и принять»."
                )
            if ad_user.is_expired:
                raise ValueError(
                    "Срок действия учетной записи AD истёк. Используйте "
                    "действие «Восстановить и принять»."
                )
            if zimbra_status != "active":
                raise ValueError(
                    "Почтовый ящик не активен. Используйте восстановление "
                    "или выберите другой ящик."
                )

        now = utcnow()
        for record in records:
            source_id = normalize_email(record.source_id)
            mapping = self.db.scalar(
                select(EmailLoginMapping).where(
                    EmailLoginMapping.worker_key == record.worker_key,
                    EmailLoginMapping.source_domain == source_id,
                )
            )
            if mapping is None:
                mapping = EmailLoginMapping(
                    worker_key=record.worker_key,
                    source_domain=source_id,
                    source_email="",
                    ad_object_guid="",
                    ad_login="",
                    zimbra_id="",
                    zimbra_email="",
                    created_by=actor,
                )
                self.db.add(mapping)
            mapping.source_email = (
                normalize_email(record.corporate_email) or normalized_email
            )
            mapping.ad_object_guid = ad_user.object_guid
            mapping.ad_login = ad_user.username
            mapping.zimbra_id = zimbra.zimbra_id
            mapping.zimbra_email = zimbra.primary_email
            mapping.last_verified_at = now
            mapping.updated_at = now
            record.ad_status = "enabled"
            record.zimbra_status = "present"
            record.reconciliation_status = "ok"
            record.reconciliation_error = ""
            record.reconciled_at = now

        action = (
            "new_employment_accounts_created"
            if provisioning_operation_id is not None
            else "new_employment_accounts_restored"
            if restore_closed
            else "new_employment_accounts_confirmed"
        )
        decision = (
            "Созданы новые учетные записи и сохранено сопоставление"
            if provisioning_operation_id is not None
            else "Существующие учетные записи восстановлены и приняты"
            if restore_closed
            else "Существующие учетные записи приняты оператором"
        )
        self.db.add(
            AuditLog(
                actor=actor,
                action=action,
                target=str(context["worker_key"]),
                result="success",
                details=json.dumps(
                    {
                        "fio": context["fio"],
                        "event_ids": context["event_ids"],
                        "ad_login": ad_user.username,
                        "ad_object_guid": ad_user.object_guid,
                        "zimbra_email": zimbra.primary_email,
                        "zimbra_id": zimbra.zimbra_id,
                    },
                    ensure_ascii=False,
                    sort_keys=True,
                ),
            )
        )
        self.db.commit()
        EmployeeArrivalService(self.db).mark_accounts_resolved(
            raw_event_ids,
            operator=actor,
            decision_details=decision,
            provisioning_operation_id=provisioning_operation_id,
        )
        return {
            "fio": context["fio"],
            "ad_login": ad_user.username,
            "zimbra_email": zimbra.primary_email,
            "restored": bool(restore_closed),
            "created": provisioning_operation_id is not None,
        }
