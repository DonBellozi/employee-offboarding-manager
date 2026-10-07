from __future__ import annotations

import json
import re
import threading
from dataclasses import dataclass

from sqlalchemy import select

from app.models import AuditLog, EmailLoginMapping, OperationStatus, ProvisioningOperation
from app.models_onec_sources import HREmploymentState
from app.services.account_requirement import AccountRequirementService
from app.services.ad import ActiveDirectoryService, wait_for_reactivated_user
from app.services.employee_arrival_accounts import EmployeeArrivalAccountService, normalize_email, normalize_fio, utcnow
from app.services.employee_arrivals import EmployeeArrivalService
from app.services.mailer import CredentialMailer, get_domain_mail_profile
from app.services.names import parse_two_line_input
from app.services.passwords import generate_mail_password
from app.services.zimbra import ZimbraService


@dataclass(frozen=True)
class MissingMailCredentials:
    full_name: str
    ad_login: str
    corporate_email: str
    mail_password: str
    operation_id: int
    dry_run: bool
    status: str
    ad_enabled: bool
    zimbra_created: bool
    credentials_mail_sent: bool
    recipient: str
    arrival_resolved: bool
    warnings: tuple[str, ...]


class ArrivalMissingMailService:
    """Complete an existing AD identity; never create/delete/reset AD accounts."""
    _locks_guard = threading.Lock()
    _locks: dict[str, threading.Lock] = {}

    def __init__(self, settings, db):
        self.settings, self.db = settings, db

    def _assert_source_active(self, record):
        state = self.db.scalar(select(HREmploymentState).where(
            HREmploymentState.worker_key == record.worker_key,
            HREmploymentState.source_id == record.source_id))
        if not record.is_present or (state is not None and
                (not state.is_present or state.status not in {"active", "scheduled"})):
            raise ValueError("Работник больше не активен в выбранной организации")

    def prepare(self, *, raw_event_ids: str, ad_login: str, source_id: str = "",
                mail_domain: str = "", expected_guid: str = "") -> dict:
        self.db.expire_all()
        AccountRequirementService(self.db).assert_import_idle()
        context = EmployeeArrivalService(self.db).registration_context(raw_event_ids)
        if not self.settings.ad_check_enabled or not self.settings.zimbra_check_enabled:
            raise ValueError("Для создания недостающей почты включите проверки AD и Zimbra")
        login = normalize_email(ad_login)
        if not re.fullmatch(r"[a-z0-9][a-z0-9._-]{0,63}", login):
            raise ValueError("Некорректный логин существующей AD")
        state = EmployeeArrivalAccountService(self.settings, self.db).inspect(raw_event_ids)
        if state["errors"]:
            raise ValueError("Проверка AD/Zimbra не завершена. Повторите после восстановления связи")
        if state["zimbra_candidates"]:
            raise ValueError("Почтовый ящик уже найден. Используйте сопоставление или восстановление, не создание дубля")
        ad = ActiveDirectoryService(self.settings)
        user = ad.get_user(login)
        if user is None or not user.object_guid:
            raise ValueError("Существующая AD не найдена либо отсутствует objectGUID")
        if expected_guid and normalize_email(user.object_guid) != normalize_email(expected_guid):
            raise ValueError("Учетная запись AD изменилась после проверки. Откройте форму заново")
        mappings = self.db.scalars(select(EmailLoginMapping)).all()
        for mapping in mappings:
            if mapping.worker_key != context["worker_key"] and (
                normalize_email(mapping.ad_object_guid) == normalize_email(user.object_guid)
                or normalize_email(mapping.ad_login) == login
            ):
                raise ValueError("Эта AD уже сопоставлена с другим работником")
        trusted_mapping = any(mapping.worker_key == context["worker_key"] and
                              normalize_email(mapping.ad_object_guid) == normalize_email(user.object_guid)
                              for mapping in mappings)
        known_logins = {normalize_email(record.login) for record in context["records"] if record.login}
        if not trusted_mapping and normalize_fio(user.display_name) != normalize_fio(context["fio"]) and login not in known_logins:
            raise ValueError("AD не сопоставлена с этим работником. Сначала уточните кадровое сопоставление")

        records = context["records"]
        selected_source = normalize_email(source_id) or records[0].source_id
        record = next((row for row in records if row.source_id == selected_source), None)
        if record is None:
            raise ValueError("Организация не относится к выбранному кадровому появлению")
        self._assert_source_active(record)
        event_ids = [event.id for event in context["events"] if event.source_id == selected_source]
        domains = list(dict.fromkeys(self.settings.zimbra_domains or [self.settings.zimbra_primary_domain]))
        domain = normalize_email(mail_domain) or (selected_source if selected_source in domains else domains[0])
        if not domain or domain not in domains:
            raise ValueError("Выберите разрешенный почтовый домен")
        primary_domain = self.settings.zimbra_primary_domain if self.settings.zimbra_domain_mode == "primary_alias" else domain
        if not primary_domain:
            raise ValueError("Не задан основной почтовый домен")
        email = f"{login}@{primary_domain}"
        zimbra = ZimbraService(self.settings)
        if zimbra.login_exists_any_domain(login, force_refresh=True):
            raise ValueError("Логин почты уже занят в Zimbra. Обновите проверку существующих учеток")
        aliases = ([f"{login}@{value}" for value in domains if value != primary_domain]
                   if self.settings.zimbra_domain_mode == "primary_alias" and self.settings.zimbra_create_aliases else [])
        if zimbra.accounts_by_addresses([email, *aliases]):
            raise ValueError("Почтовый адрес или один из его алиасов уже занят")
        return {"context": context, "user": user, "record": record, "domains": domains,
                "domain": domain, "email": email, "aliases": aliases,
                "source_id": selected_source, "raw_event_ids": ",".join(map(str, event_ids))}

    def create(self, *, raw_event_ids: str, ad_login: str, expected_guid: str,
               source_id: str, mail_domain: str, actor: str) -> MissingMailCredentials:
        if not str(expected_guid or "").strip():
            raise ValueError("Откройте форму заново: отсутствует objectGUID существующей AD")
        # Lock before any lookup: a double POST may not race a freshly created mailbox.
        key = normalize_email(ad_login)
        with self._locks_guard:
            lock = self._locks.setdefault(key, threading.Lock())
        if not lock.acquire(blocking=False):
            raise ValueError("Создание почты для этой AD уже выполняется")
        try:
            prepared = self.prepare(raw_event_ids=raw_event_ids, ad_login=ad_login,
                                    expected_guid=expected_guid, source_id=source_id, mail_domain=mail_domain)
            return self._create_locked(prepared, actor=actor)
        finally:
            lock.release()

    def _create_locked(self, prepared: dict, *, actor: str) -> MissingMailCredentials:
        settings, db = self.settings, self.db
        user, record = prepared["user"], prepared["record"]
        person = parse_two_line_input(record.fio)
        profile = get_domain_mail_profile(db, settings, prepared["domain"])
        password = generate_mail_password(settings.mail_password_length, settings.mail_password_specials)
        if not settings.dry_run:
            requirement_service = AccountRequirementService(db)
            case = requirement_service.find(EmployeeArrivalService.parse_event_ids(prepared["raw_event_ids"]))
            if case is not None:
                requirement_service.cancel(case, "Дополняется существующая AD: это не решение о новой корпоративной УЗ", actor=actor)
        operation = ProvisioningOperation(operation_kind="mail_only", operator_username=actor,
            last_name=person.last_name, first_name=person.first_name, middle_name=person.middle_name,
            personal_email=record.personal_email or "", login=user.username, corporate_email=prepared["email"],
            mail_domain=prepared["email"].split("@", 1)[1], status=OperationStatus.RUNNING)
        db.add(operation)
        db.commit()
        zimbra, ad = ZimbraService(settings), ActiveDirectoryService(settings)
        warnings = []
        resolved = False
        mailbox_confirmed = False
        recipient = record.personal_email.strip() or prepared["email"]

        def warning(message, exc):
            warnings.append(f"{message}: {exc}".replace(password, "[скрыто]")[:2000])

        try:
            db.expire_all()
            AccountRequirementService(db).assert_import_idle()
            EmployeeArrivalService(db).registration_context(prepared["raw_event_ids"])
            self._assert_source_active(record)
            result = zimbra.create_account(login=user.username, domain=prepared["domain"], password=password,
                last_name=person.last_name, first_name=person.first_name, middle_name=person.middle_name)
            operation.zimbra_created = True
            operation.corporate_email = result.primary_email
            db.commit()  # Record the external result before any further action.
            if settings.dry_run:
                mailbox_confirmed = True
            else:
                mailbox = zimbra.account_by_address(result.primary_email)
                if mailbox is None or not mailbox.zimbra_id or normalize_email(mailbox.account_status) != "active":
                    raise RuntimeError("Zimbra не подтвердила активный ящик и его zimbraId")
                # Save stable links even if AD restoration later fails; no generated login in HR.
                mapping = db.scalar(select(EmailLoginMapping).where(
                    EmailLoginMapping.worker_key == record.worker_key,
                    EmailLoginMapping.source_domain == record.source_id))
                if mapping is None:
                    mapping = EmailLoginMapping(worker_key=record.worker_key, source_domain=record.source_id,
                        source_email="", ad_object_guid="", ad_login="", zimbra_id="", zimbra_email="", created_by=actor)
                    db.add(mapping)
                mapping.source_email = record.corporate_email or result.primary_email
                mapping.ad_object_guid, mapping.ad_login = user.object_guid, user.username
                mapping.zimbra_id, mapping.zimbra_email = mailbox.zimbra_id, mailbox.primary_email
                mapping.last_verified_at = utcnow()
                record.zimbra_status = "present"
                record.ad_status = "enabled" if user.is_enabled and not user.is_expired else "disabled"
                record.reconciliation_status = "issue"
                db.commit()
                mailbox_confirmed = True
        except Exception as exc:
            db.rollback()
            warning("Создание или проверка почты завершились не полностью", exc)

        if mailbox_confirmed and not settings.dry_run:
            try:
                db.expire_all()
                AccountRequirementService(db).assert_import_idle()
                EmployeeArrivalService(db).registration_context(prepared["raw_event_ids"])
                self._assert_source_active(record)
                fresh_user = ad.get_user_by_object_guid(user.object_guid)
                if fresh_user is None or normalize_email(fresh_user.username) != normalize_email(user.username):
                    raise RuntimeError("Существующая AD изменилась или исчезла; восстановление остановлено")
                if not fresh_user.is_enabled or fresh_user.is_expired:
                    ad.reactivate_existing_user(fresh_user.distinguished_name)
                    fresh_user = wait_for_reactivated_user(ad, object_guid=user.object_guid, username=user.username)
                if (fresh_user is None or not fresh_user.is_enabled or fresh_user.is_expired
                        or normalize_email(fresh_user.object_guid) != normalize_email(user.object_guid)):
                    raise RuntimeError("AD не подтвердил восстановление учетной записи")
                operation.ad_enabled = True
                db.commit()
                EmployeeArrivalAccountService(settings, db).resolve(
                    raw_event_ids=prepared["raw_event_ids"], ad_login=user.username,
                    zimbra_email=operation.corporate_email, actor=actor, restore_closed=False,
                    provisioning_operation_id=operation.id)
                resolved = True
            except Exception as exc:
                db.rollback()
                warning("Почта создана, но восстановление/сопоставление AD не завершено", exc)

        if mailbox_confirmed and not settings.dry_run:
            try:
                db.expire_all()
                AccountRequirementService(db).assert_import_idle()
                self._assert_source_active(record)
                CredentialMailer(settings).send_mail_credentials(profile=profile, personal_email=recipient,
                    full_name=record.fio, corporate_email=operation.corporate_email, mail_password=password)
                operation.personal_mail_sent = True
                db.commit()
            except Exception as exc:
                db.rollback()
                warning("Реквизиты почты не отправлены; сохраните их с этого экрана", exc)

        if settings.dry_run:
            warnings.append("DRY_RUN: существующая AD не изменена, кадровое уведомление осталось открытым")
        operation.status = (OperationStatus.SUCCESS if resolved and operation.personal_mail_sent
                            else OperationStatus.PARTIAL if operation.zimbra_created else OperationStatus.FAILED)
        operation.error_message = "\n".join(warnings)[:4000]
        operation.completed_at = utcnow()
        db.add(AuditLog(actor=actor, action="provision_mail_existing_ad", target=operation.corporate_email,
            result=operation.status.value, details=json.dumps({"operation_id": operation.id,
                "ad_login": user.username, "ad_reused": True, "arrival_resolved": resolved}, ensure_ascii=False)))
        db.commit()
        return MissingMailCredentials(full_name=record.fio, ad_login=user.username,
            corporate_email=operation.corporate_email, mail_password=password if operation.zimbra_created else "",
            operation_id=operation.id, dry_run=settings.dry_run, status=operation.status.value,
            ad_enabled=operation.ad_enabled, zimbra_created=operation.zimbra_created,
            credentials_mail_sent=operation.personal_mail_sent, recipient=recipient,
            arrival_resolved=resolved, warnings=tuple(warnings))
