from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import threading
import re
from urllib.parse import urlencode

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import Settings
from app.models import (
    ADProvisioningOperation,
    AuditLog,
    EmailLoginMapping,
    HRSourceRecord,
    OperationStatus,
    ProvisioningOperation,
)
from app.services.ad import ActiveDirectoryService
from app.services.email_login_mapping import EmailLoginMappingService
from app.services.mailer import CredentialMailer, get_domain_mail_profile
from app.services.names import parse_two_line_input, transliterate
from app.services.passwords import generate_ad_password, generate_mail_password
from app.services.zimbra import ZimbraService


@dataclass(frozen=True)
class ProvisioningInput:
    last_name: str
    first_name: str
    middle_name: str
    personal_email: str
    login: str
    mail_domain: str


@dataclass(frozen=True)
class ADNameCandidate:
    username: str
    display_name: str
    email: str
    is_enabled: bool
    object_guid: str
    mapping_url: str


@dataclass(frozen=True)
class ExistingMailboxADPreflight:
    record_id: int
    worker_key: str
    source_id: str
    full_name: str
    corporate_email: str
    login: str
    zimbra_primary_email: str
    zimbra_login: str
    exact_matches: tuple[ADNameCandidate, ...]
    name_candidates: tuple[ADNameCandidate, ...]
    has_mapping: bool
    can_create: bool
    block_reason: str


@dataclass(frozen=True)
class ADOnlyProvisioningCredentials:
    full_name: str
    corporate_email: str
    ad_login: str
    ad_password: str
    operation_id: int
    dry_run: bool
    status: str
    ad_created: bool
    ad_enabled: bool
    credentials_mail_sent: bool
    registry_updated: bool
    warnings: tuple[str, ...]


@dataclass(frozen=True)
class ProvisioningCredentials:
    full_name: str
    corporate_email: str
    mail_password: str
    ad_login: str
    ad_password: str
    aliases: tuple[str, ...]
    operation_id: int
    dry_run: bool
    status: str
    ad_created: bool
    ad_enabled: bool
    zimbra_created: bool
    personal_email_provided: bool
    mail_credentials_recipient: str
    personal_mail_sent: bool
    corporate_mail_sent: bool
    warnings: tuple[str, ...]


class ProvisioningService:
    # Защита от двойного POST по одной и той же учетной записи.
    # UI блокирует кнопку сразу, но сервер не должен доверять браузеру:
    # повторный запрос, Enter, обновление страницы или два окна не должны
    # одновременно запускать создание одного sAMAccountName.
    _ad_only_locks_guard = threading.Lock()
    _ad_only_locks: dict[str, threading.Lock] = {}

    @classmethod
    def _ad_only_lock_for_login(cls, login: str) -> threading.Lock:
        normalized = str(login or "").strip().lower()
        with cls._ad_only_locks_guard:
            lock = cls._ad_only_locks.get(normalized)
            if lock is None:
                lock = threading.Lock()
                cls._ad_only_locks[normalized] = lock
            return lock

    @classmethod
    def _release_ad_only_lock(cls, login: str, lock: threading.Lock) -> None:
        lock.release()
        normalized = str(login or "").strip().lower()
        with cls._ad_only_locks_guard:
            current = cls._ad_only_locks.get(normalized)
            # Удаляем только свободный именно тот lock, который создавали.
            # Если другой запрос уже успел его захватить, запись остается.
            if current is lock and not lock.locked():
                cls._ad_only_locks.pop(normalized, None)

    def __init__(self, settings: Settings):
        self.settings = settings
        self.ad = ActiveDirectoryService(settings)
        self.zimbra = ZimbraService(settings)
        self.mailer = CredentialMailer(settings)

    def check_login(
        self,
        login: str,
        *,
        force_refresh: bool = False,
    ) -> dict[str, bool]:
        # Проверяем оба источника, чтобы оператор видел точную причину
        # занятости логина, даже если он существует одновременно в AD и Zimbra.
        ad_exists = self.ad.login_exists(login)
        zimbra_exists = self.zimbra.login_exists_any_domain(
            login,
            force_refresh=force_refresh,
        )
        return {
            "ad": ad_exists,
            "zimbra": zimbra_exists,
        }

    def check_logins(
        self,
        logins: list[str],
        *,
        force_refresh: bool = False,
        background: bool = False,
    ) -> list[dict[str, object]]:
        normalized = list(dict.fromkeys(login.strip().lower() for login in logins if login.strip()))
        if not normalized:
            return []

        ad_existing = self.ad.logins_exist(normalized)
        # После перехода на одну пакетную zmprov-проверку нет необходимости
        # пропускать кандидатов, занятых в AD. Проверка всех логинов дает
        # оператору полный список источников занятости.
        zimbra_existing = self.zimbra.logins_exist_any_domain(
            normalized,
            force_refresh=force_refresh,
            background=background,
        )

        return [
            {
                "login": login,
                "ad": login in ad_existing,
                "zimbra": login in zimbra_existing,
                "free": login not in ad_existing and login not in zimbra_existing,
            }
            for login in normalized
        ]

    @staticmethod
    def _login_from_existing_email(corporate_email: str) -> str:
        normalized = str(corporate_email or "").strip().lower()
        if normalized.count("@") != 1:
            raise ValueError("В кадровом реестре указан некорректный корпоративный e-mail")
        local_part, _ = normalized.split("@", 1)
        if not local_part:
            raise ValueError("В корпоративном e-mail отсутствует логин")

        # В этом сценарии логин не транслитерируется и не исправляется:
        # он должен в точности соответствовать уже существующей почте.
        # Проверяем только ограничения, без которых AD физически не создаст
        # sAMAccountName.
        forbidden = set('/\\\\[]:;|=,+*?<>@"')
        if len(local_part) > 20:
            raise ValueError(
                "Логин из существующего e-mail длиннее 20 символов. "
                "Создать sAMAccountName без изменения логина невозможно."
            )
        if any(
            char.isspace()
            or ord(char) < 32
            or char in forbidden
            for char in local_part
        ):
            raise ValueError(
                "Логин из существующего e-mail содержит символы, "
                "недопустимые для sAMAccountName. Автоматически менять логин нельзя."
            )
        return local_part

    @staticmethod
    def _fio_parts(full_name: str) -> tuple[str, str, str]:
        parsed = parse_two_line_input(full_name)
        return parsed.last_name, parsed.first_name, parsed.middle_name

    @staticmethod
    def _name_tokens(value: str) -> list[str]:
        return [
            token.casefold()
            for token in re.findall(r"[A-Za-zА-Яа-яЁё]+", str(value or ""))
            if token
        ]

    @classmethod
    def _name_candidate_score(
        cls,
        *,
        last_name: str,
        first_name: str,
        middle_name: str,
        display_name: str,
    ) -> float:
        tokens = cls._name_tokens(display_name)
        if not tokens:
            return 0.0

        last = last_name.casefold()
        first = first_name.casefold()
        middle = middle_name.casefold()
        token_set = set(tokens)

        surname_match = last in token_set
        first_full = first in token_set
        first_initial = any(
            len(token) == 1 and token == first[:1]
            for token in tokens
        )
        middle_full = bool(middle and middle in token_set)
        middle_initial = bool(
            middle
            and any(
                len(token) == 1 and token == middle[:1]
                for token in tokens
            )
        )

        if surname_match:
            score = 4.0
            if first_full:
                score += 3.0
            elif first_initial:
                score += 1.5
            else:
                return 0.0

            if middle_full:
                score += 2.0
            elif middle_initial:
                score += 1.0
            return score

        # Смена фамилии: автоматически связь не устанавливаем, но показываем
        # сильного кандидата оператору. Без совпадения фамилии требуем полное
        # имя и совпадение отчества хотя бы инициалом.
        if first_full and middle_full:
            return 6.5
        if first_full and middle_initial:
            return 5.5
        return 0.0

    @staticmethod
    def _candidate_to_view(user, corporate_email: str) -> ADNameCandidate:
        return ADNameCandidate(
            username=user.username,
            display_name=user.display_name,
            email=user.email,
            is_enabled=user.is_enabled,
            object_guid=user.object_guid,
            mapping_url=(
                "/settings/email-login-mapping?"
                + urlencode(
                    {
                        "email": corporate_email,
                        "ad_login": user.username,
                    }
                )
            ),
        )

    def _name_candidates(
        self,
        *,
        full_name: str,
        corporate_email: str,
        excluded_usernames: set[str] | None = None,
    ) -> tuple[ADNameCandidate, ...]:
        last_name, first_name, middle_name = self._fio_parts(full_name)
        excluded = {
            value.casefold()
            for value in (excluded_usernames or set())
            if value
        }

        # Ищем не только по текущей фамилии. Имя и отчество нужны для
        # случаев, когда в AD осталась старая фамилия.
        search_terms = [last_name, first_name]
        if middle_name:
            search_terms.append(middle_name)

        candidates = []
        seen_candidates: set[str] = set()
        for term in search_terms:
            for user in self.ad.search_users(term, limit=50):
                key = (user.object_guid or user.username).casefold()
                if key in seen_candidates:
                    continue
                seen_candidates.add(key)
                candidates.append(user)

        scored: list[tuple[float, object]] = []
        for user in candidates:
            if user.username.casefold() in excluded:
                continue
            score = self._name_candidate_score(
                last_name=last_name,
                first_name=first_name,
                middle_name=middle_name,
                display_name=user.display_name,
            )
            if score >= 5.5:
                scored.append((score, user))

        scored.sort(
            key=lambda item: (
                -item[0],
                not item[1].is_enabled,
                item[1].display_name.casefold(),
                item[1].username,
            )
        )
        return tuple(
            self._candidate_to_view(user, corporate_email)
            for _, user in scored[:12]
        )

    def prepare_ad_for_existing_mailbox(
        self,
        db: Session,
        record_id: int,
    ) -> ExistingMailboxADPreflight:
        record = db.get(HRSourceRecord, record_id)
        if record is None or not record.is_present:
            raise ValueError("Работник отсутствует в текущем кадровом реестре")

        corporate_email = record.corporate_email.strip().lower()
        if not corporate_email:
            raise ValueError("У работника нет корпоративного e-mail")

        login = self._login_from_existing_email(corporate_email)
        domain = corporate_email.rsplit("@", 1)[1]
        get_domain_mail_profile(db, self.settings, domain)

        mappings = db.scalars(
            select(EmailLoginMapping).where(
                EmailLoginMapping.worker_key == record.worker_key
            )
        ).all()
        # Сопоставление только с Zimbra как раз означает, что AD может быть
        # недостающей частью комплекта. Блокируем создание лишь когда в
        # сохраненном соответствии уже указана AD-учетка.
        has_mapping = any(
            str(mapping.ad_object_guid or "").strip()
            or str(mapping.ad_login or "").strip()
            for mapping in mappings
        )

        zimbra_identity = self.zimbra.account_by_address(corporate_email)
        if zimbra_identity is None:
            raise ValueError(
                "Существующий адрес не найден в Zimbra. "
                "Сценарий создания только AD недоступен."
            )

        exact_by_login = self.ad.get_user(login)
        exact_by_email = self.ad.users_by_email(corporate_email, limit=10)

        exact_users = []
        seen: set[str] = set()
        for user in [exact_by_login, *exact_by_email]:
            if user is None:
                continue
            key = user.object_guid or user.username.casefold()
            if key in seen:
                continue
            seen.add(key)
            exact_users.append(user)

        exact_matches = tuple(
            self._candidate_to_view(user, corporate_email)
            for user in exact_users
        )

        excluded = {user.username for user in exact_users}
        name_candidates: tuple[ADNameCandidate, ...] = ()
        if not has_mapping and not exact_matches:
            name_candidates = self._name_candidates(
                full_name=record.fio,
                corporate_email=corporate_email,
                excluded_usernames=excluded,
            )

        block_reason = ""
        if has_mapping:
            block_reason = (
                "Для работника уже существует явное сопоставление "
                "с учетной записью AD. Создание новой учетной записи запрещено."
            )
        elif exact_matches:
            block_reason = (
                "В AD уже найдена учетная запись по логину или корпоративному e-mail. "
                "Сначала используйте сопоставление, чтобы не создать дубль."
            )

        return ExistingMailboxADPreflight(
            record_id=record.id,
            worker_key=record.worker_key,
            source_id=record.source_id,
            full_name=record.fio,
            corporate_email=corporate_email,
            login=login,
            zimbra_primary_email=zimbra_identity.primary_email,
            zimbra_login=zimbra_identity.login,
            exact_matches=exact_matches,
            name_candidates=name_candidates,
            has_mapping=has_mapping,
            can_create=not block_reason,
            block_reason=block_reason,
        )

    def _update_registry_after_ad_only(
        self,
        *,
        db: Session,
        record: HRSourceRecord,
        corporate_email: str,
        login: str,
        operator: str,
        warnings: list[str],
    ) -> bool:
        if self.settings.dry_run:
            return False

        now = datetime.now(timezone.utc)
        try:
            ad_user = self.ad.get_user(login)
            zimbra_identity = self.zimbra.account_by_address(corporate_email)
            if ad_user is None:
                record.ad_status = "missing"
                record.reconciliation_status = "issue"
                record.reconciliation_error = (
                    "После создания учетная запись AD не найдена повторной проверкой"
                )
                record.reconciled_at = now
                db.commit()
                return False

            record.ad_status = "enabled" if ad_user.is_enabled else "disabled"

            if zimbra_identity is None:
                record.zimbra_status = "missing"
                record.reconciliation_status = "issue"
                record.reconciliation_error = (
                    "После создания существующий адрес Zimbra не найден"
                )
                record.reconciled_at = now
                db.commit()
                return False

            record.zimbra_status = (
                "present"
                if corporate_email in zimbra_identity.addresses
                else "address_mismatch"
            )

            linked = zimbra_identity.login.casefold() == ad_user.username.casefold()
            if not linked:
                try:
                    result = EmailLoginMappingService(
                        self.settings,
                        db,
                    ).add_manual(
                        corporate_email,
                        ad_user.username,
                        operator,
                    )
                    linked = result["status"] in {
                        "created",
                        "updated",
                        "not_needed",
                    }
                except Exception as exc:
                    warnings.append(
                        "AD создана, но автоматическое сопоставление "
                        f"с Zimbra не сохранено: {exc}"
                    )

            if (
                record.ad_status == "enabled"
                and record.zimbra_status == "present"
                and linked
            ):
                record.reconciliation_status = "ok"
                record.reconciliation_error = ""
            else:
                record.reconciliation_status = "issue"
                if not record.reconciliation_error:
                    record.reconciliation_error = (
                        "После создания требуется повторная проверка сопоставления"
                    )

            record.login = login
            record.reconciled_at = now
            db.commit()
            return record.reconciliation_status == "ok"
        except Exception as exc:
            db.rollback()
            warnings.append(
                "AD создана, но не удалось обновить кадровый реестр: "
                f"{exc}"
            )
            return False

    def confirm_ad_candidate(
        self,
        db: Session,
        operator: str,
        record_id: int,
        ad_login: str,
    ) -> dict:
        """Подтвердить, что найденная AD-учетка принадлежит работнику 1С."""
        preflight = self.prepare_ad_for_existing_mailbox(db, record_id)
        normalized_login = str(ad_login or "").strip().lower()
        if not normalized_login:
            raise ValueError("Не указан логин AD")

        candidates = {
            candidate.username.casefold(): candidate
            for candidate in (
                *preflight.exact_matches,
                *preflight.name_candidates,
            )
        }
        candidate = candidates.get(normalized_login.casefold())
        if candidate is None:
            raise ValueError(
                "Учетная запись больше не входит в найденные кандидаты. "
                "Обновите страницу и повторите проверку."
            )

        record = db.get(HRSourceRecord, record_id)
        if record is None or not record.is_present:
            raise ValueError("Работник отсутствует в текущем кадровом реестре")

        ad_user = self.ad.get_user(candidate.username)
        if ad_user is None:
            raise ValueError(
                f"AD: учетная запись {candidate.username} больше не найдена"
            )

        zimbra_identity = self.zimbra.account_by_address(
            preflight.corporate_email
        )
        if zimbra_identity is None:
            raise ValueError(
                "Zimbra: существующий корпоративный адрес больше не найден"
            )

        mapping_result = EmailLoginMappingService(
            self.settings,
            db,
        ).save_confirmed_identity(
            record=record,
            ad_user=ad_user,
            zimbra=zimbra_identity,
            actor=operator,
        )

        now = datetime.now(timezone.utc)
        record.ad_status = "enabled" if ad_user.is_enabled else "disabled"
        record.zimbra_status = (
            "present"
            if preflight.corporate_email in zimbra_identity.addresses
            else "address_mismatch"
        )

        if record.ad_status == "enabled" and record.zimbra_status == "present":
            record.reconciliation_status = "ok"
            record.reconciliation_error = ""
        else:
            record.reconciliation_status = "issue"
            reasons = []
            if record.ad_status == "disabled":
                reasons.append("Подтвержденная учетная запись AD отключена")
            if record.zimbra_status != "present":
                reasons.append(
                    "Корпоративный e-mail не привязан к найденному ящику Zimbra"
                )
            record.reconciliation_error = "\n".join(reasons)

        record.reconciled_at = now
        db.add(
            AuditLog(
                actor=operator,
                action="confirm_ad_candidate",
                target=preflight.corporate_email,
                result="success",
                details=(
                    f"AD={ad_user.username}; "
                    f"objectGUID={ad_user.object_guid}; "
                    f"mapping={mapping_result['status']}"
                )[:1000],
            )
        )
        db.commit()

        return {
            "fio": record.fio,
            "email": preflight.corporate_email,
            "ad_login": ad_user.username,
            "ad_display_name": ad_user.display_name,
            "mapping_status": mapping_result["status"],
            "reconciliation_status": record.reconciliation_status,
            "ad_enabled": ad_user.is_enabled,
        }


    def provision_ad_for_existing_mailbox(
        self,
        db: Session,
        operator: str,
        record_id: int,
        *,
        confirm_name_candidates: bool = False,
    ) -> ADOnlyProvisioningCredentials:
        # Первая проверка нужна, чтобы определить точный логин из существующего
        # e-mail. Затем берем серверную блокировку именно на этот sAMAccountName.
        preflight = self.prepare_ad_for_existing_mailbox(db, record_id)
        if not preflight.can_create:
            raise RuntimeError(preflight.block_reason)
        if preflight.name_candidates and not confirm_name_candidates:
            raise RuntimeError(
                "В AD найдены возможные совпадения по ФИО. "
                "Подтвердите, что это другие люди, либо выполните сопоставление."
            )

        creation_lock = self._ad_only_lock_for_login(preflight.login)
        if not creation_lock.acquire(blocking=False):
            raise RuntimeError(
                "Создание этой учетной записи AD уже выполняется. "
                "Дождитесь завершения текущей операции."
            )

        try:
            # Критически важная повторная проверка уже ВНУТРИ блокировки.
            # Если первый запрос успел создать AD, второй увидит существующую
            # учетную запись и завершится без повторного создания.
            preflight = self.prepare_ad_for_existing_mailbox(db, record_id)
            if not preflight.can_create:
                raise RuntimeError(preflight.block_reason)
            if preflight.name_candidates and not confirm_name_candidates:
                raise RuntimeError(
                    "В AD найдены возможные совпадения по ФИО. "
                    "Подтвердите, что это другие люди, либо выполните сопоставление."
                )

            return self._provision_ad_for_existing_mailbox_locked(
                db,
                operator,
                record_id,
                preflight=preflight,
            )
        finally:
            self._release_ad_only_lock(preflight.login, creation_lock)

    def _provision_ad_for_existing_mailbox_locked(
        self,
        db: Session,
        operator: str,
        record_id: int,
        *,
        preflight: ExistingMailboxADPreflight,
    ) -> ADOnlyProvisioningCredentials:
        record = db.get(HRSourceRecord, record_id)
        if record is None or not record.is_present:
            raise RuntimeError("Работник больше не присутствует в кадровом реестре")

        last_name, first_name, middle_name = self._fio_parts(preflight.full_name)
        domain = preflight.corporate_email.rsplit("@", 1)[1]
        mail_profile = get_domain_mail_profile(db, self.settings, domain)

        # Пароль генерируется по тем же правилам, что и в обычном разделе
        # «Создание». Логин, напротив, НЕ генерируется: он взят из e-mail.
        ad_password_candidates = [
            generate_ad_password(
                transliterate(first_name),
                transliterate(last_name),
                self.settings.ad_password_min_length,
                self.settings.ad_password_max_length,
                self.settings.ad_password_specials,
            )
            for _ in range(10)
        ]
        ad_password = ""

        operation = ADProvisioningOperation(
            worker_key=record.worker_key,
            source_id=record.source_id,
            operator_username=operator,
            full_name=preflight.full_name,
            login=preflight.login,
            corporate_email=preflight.corporate_email,
            status=OperationStatus.RUNNING,
        )
        db.add(operation)
        db.commit()
        db.refresh(operation)

        warnings: list[str] = []
        ad_dn = ""

        try:
            ad_result = self.ad.create_disabled_user(
                login=preflight.login,
                password_candidates=ad_password_candidates,
                last_name=last_name,
                first_name=first_name,
                middle_name=middle_name,
                corporate_email=preflight.corporate_email,
            )
            ad_dn = ad_result.dn
            ad_password = ad_result.accepted_password
            operation.ad_created = True
            db.commit()
        except Exception as exc:
            warnings.append(f"Учетная запись AD не создана: {exc}")

        if operation.ad_created:
            try:
                self.ad.enable_user(ad_dn)
                operation.ad_enabled = True
                db.commit()
            except Exception as exc:
                warnings.append(
                    f"Учетная запись AD создана, но осталась отключенной: {exc}"
                )

        if operation.ad_enabled:
            try:
                self.mailer.send_ad_credentials(
                    profile=mail_profile,
                    corporate_email=preflight.corporate_email,
                    full_name=preflight.full_name,
                    ad_login=preflight.login,
                    ad_password=ad_password,
                )
                operation.credentials_mail_sent = True
                db.commit()
            except Exception as exc:
                warnings.append(
                    "Учетная запись AD создана, но письмо с реквизитами "
                    f"не отправлено: {exc}"
                )

        if operation.ad_created:
            operation.registry_updated = self._update_registry_after_ad_only(
                db=db,
                record=record,
                corporate_email=preflight.corporate_email,
                login=preflight.login,
                operator=operator,
                warnings=warnings,
            )

        complete = (
            operation.ad_created
            and operation.ad_enabled
            and operation.credentials_mail_sent
            and (operation.registry_updated or self.settings.dry_run)
        )
        operation.status = (
            OperationStatus.SUCCESS
            if complete
            else OperationStatus.FAILED
            if not operation.ad_created
            else OperationStatus.PARTIAL
        )
        operation.error_message = "\n".join(warnings)[:4000]
        operation.completed_at = datetime.now(timezone.utc)

        db.add(
            AuditLog(
                actor=operator,
                action="provision_ad_existing_mailbox",
                target=preflight.corporate_email,
                result=operation.status.value,
                details=(
                    f"worker_key={record.worker_key}; "
                    f"login={preflight.login}; "
                    f"mail_sent={operation.credentials_mail_sent}; "
                    f"registry_updated={operation.registry_updated}"
                )[:1000],
            )
        )
        db.commit()

        return ADOnlyProvisioningCredentials(
            full_name=preflight.full_name,
            corporate_email=preflight.corporate_email,
            ad_login=preflight.login,
            ad_password=ad_password,
            operation_id=operation.id,
            dry_run=self.settings.dry_run,
            status=operation.status.value,
            ad_created=operation.ad_created,
            ad_enabled=operation.ad_enabled,
            credentials_mail_sent=operation.credentials_mail_sent,
            registry_updated=operation.registry_updated,
            warnings=tuple(warnings),
        )

    def provision(self, db: Session, operator: str, data: ProvisioningInput) -> ProvisioningCredentials:
        # Выбранный логин уже проверен оператором. Полный поиск альтернатив
        # больше не нужен: останавливаем его и сразу выполняем финальную
        # проверку только выбранного логина.
        self.zimbra.cancel_background_checks()

        # Непосредственно перед созданием не доверяем кратковременному
        # кэшу Zimbra: итоговая проверка всегда выполняется заново.
        availability = self.check_login(data.login, force_refresh=True)
        if availability["ad"] or availability["zimbra"]:
            occupied = ", ".join(name for name, value in availability.items() if value)
            raise RuntimeError(f"Логин уже занят: {occupied}")

        full_name = " ".join(part for part in [data.last_name, data.first_name, data.middle_name] if part)
        mail_profile = get_domain_mail_profile(
            db,
            self.settings,
            data.mail_domain,
        )
        mail_password = generate_mail_password(
            self.settings.mail_password_length,
            self.settings.mail_password_specials,
        )
        ad_password_candidates = [
            generate_ad_password(
                transliterate(data.first_name),
                transliterate(data.last_name),
                self.settings.ad_password_min_length,
                self.settings.ad_password_max_length,
                self.settings.ad_password_specials,
            )
            for _ in range(10)
        ]
        ad_password = ad_password_candidates[0]

        primary_domain = (
            self.settings.zimbra_primary_domain
            if self.settings.zimbra_domain_mode == "primary_alias"
            else data.mail_domain
        )
        corporate_email = f"{data.login}@{primary_domain}"
        operation = ProvisioningOperation(
            operator_username=operator,
            last_name=data.last_name,
            first_name=data.first_name,
            middle_name=data.middle_name,
            personal_email=data.personal_email,
            login=data.login,
            corporate_email=corporate_email,
            mail_domain=primary_domain,
            status=OperationStatus.RUNNING,
        )
        db.add(operation)
        db.commit()
        db.refresh(operation)

        ad_dn = ""
        aliases: tuple[str, ...] = ()
        warnings: list[str] = []

        # 1. AD создается отключенным. Если Zimbra не создастся, вход в AD
        # не будет доступен.
        try:
            ad_result = self.ad.create_disabled_user(
                login=data.login,
                password_candidates=ad_password_candidates,
                last_name=data.last_name,
                first_name=data.first_name,
                middle_name=data.middle_name,
                corporate_email=corporate_email,
            )
            ad_dn = ad_result.dn
            ad_password = ad_result.accepted_password
            operation.ad_created = True
            db.commit()
        except Exception as exc:
            warnings.append(f"Учетная запись AD не создана: {exc}")

        # 2. Почтовый ящик создается только при успешном создании заготовки AD.
        if operation.ad_created:
            try:
                zimbra_result = self.zimbra.create_account(
                    login=data.login,
                    domain=data.mail_domain,
                    password=mail_password,
                    last_name=data.last_name,
                    first_name=data.first_name,
                    middle_name=data.middle_name,
                )
                corporate_email = zimbra_result.primary_email
                aliases = zimbra_result.aliases
                operation.corporate_email = corporate_email
                operation.zimbra_created = True
                db.commit()
            except Exception as exc:
                warnings.append(f"Почтовый ящик Zimbra не создан: {exc}")
                if self.settings.rollback_ad_on_zimbra_failure and ad_dn:
                    try:
                        self.ad.delete_user(ad_dn)
                        operation.ad_created = False
                        warnings.append("Отключенная заготовка AD удалена согласно настройке rollback.")
                    except Exception as rollback_exc:
                        warnings.append(f"Не удалось удалить заготовку AD: {rollback_exc}")
                    db.commit()

        # 3. AD включается только после успешного создания Zimbra.
        if operation.ad_created and operation.zimbra_created:
            try:
                self.ad.enable_user(ad_dn)
                operation.ad_enabled = True
                db.commit()
            except Exception as exc:
                warnings.append(f"Учетная запись AD создана, но осталась отключенной: {exc}")

        # 4. Отправка писем не откатывает созданные учетные записи.
        # При отсутствии личного адреса письмо с реквизитами почты
        # отправляется на только что созданную именную корпоративную почту.
        mail_credentials_recipient = (
            data.personal_email or corporate_email
        )
        if operation.zimbra_created:
            try:
                self.mailer.send_mail_credentials(
                    profile=mail_profile,
                    personal_email=mail_credentials_recipient,
                    full_name=full_name,
                    corporate_email=corporate_email,
                    mail_password=mail_password,
                )
                operation.personal_mail_sent = True
                db.commit()
            except Exception as exc:
                warnings.append(
                    "Реквизиты почты не отправлены на "
                    f"{mail_credentials_recipient}: {exc}"
                )

        if operation.ad_enabled and operation.zimbra_created:
            try:
                self.mailer.send_ad_credentials(
                    profile=mail_profile,
                    corporate_email=corporate_email,
                    full_name=full_name,
                    ad_login=data.login,
                    ad_password=ad_password,
                )
                operation.corporate_mail_sent = True
                db.commit()
            except Exception as exc:
                warnings.append(f"Реквизиты AD не отправлены на корпоративную почту: {exc}")

        complete = all(
            [
                operation.ad_created,
                operation.ad_enabled,
                operation.zimbra_created,
                operation.personal_mail_sent,
                operation.corporate_mail_sent,
            ]
        )
        nothing_created = not operation.ad_created and not operation.zimbra_created
        operation.status = (
            OperationStatus.SUCCESS
            if complete
            else OperationStatus.FAILED
            if nothing_created
            else OperationStatus.PARTIAL
        )
        operation.error_message = "\n".join(warnings)[:4000]
        operation.completed_at = datetime.now(timezone.utc)
        db.add(
            AuditLog(
                actor=operator,
                action="provision",
                target=corporate_email,
                result=operation.status.value,
                details="; ".join(warnings)[:1000],
            )
        )
        db.commit()

        return ProvisioningCredentials(
            full_name=full_name,
            corporate_email=corporate_email,
            mail_password=mail_password,
            ad_login=data.login,
            ad_password=ad_password,
            aliases=aliases,
            operation_id=operation.id,
            dry_run=self.settings.dry_run,
            status=operation.status.value,
            ad_created=operation.ad_created,
            ad_enabled=operation.ad_enabled,
            zimbra_created=operation.zimbra_created,
            personal_email_provided=bool(data.personal_email),
            mail_credentials_recipient=mail_credentials_recipient,
            personal_mail_sent=operation.personal_mail_sent,
            corporate_mail_sent=operation.corporate_mail_sent,
            warnings=tuple(warnings),
        )
