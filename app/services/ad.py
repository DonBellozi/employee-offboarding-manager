from __future__ import annotations

import ssl
import uuid
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo

from ldap3 import (
    ALL,
    BASE,
    NTLM,
    SIMPLE,
    Connection,
    MODIFY_ADD,
    MODIFY_DELETE,
    MODIFY_REPLACE,
    Server,
    Tls,
)
from ldap3.core.exceptions import LDAPException
from ldap3.utils.conv import escape_bytes, escape_filter_chars
from ldap3.utils.dn import escape_rdn

from app.config import Settings


@dataclass(frozen=True)
class ADCreateResult:
    dn: str
    login: str
    upn: str
    accepted_password: str


@dataclass(frozen=True)
class ADDirectoryUser:
    username: str
    display_name: str
    email: str
    distinguished_name: str
    is_enabled: bool
    object_guid: str = ""
    is_expired: bool = False


class ActiveDirectoryService:
    UAC_ACCOUNTDISABLE = 0x0002
    UAC_NORMAL_ACCOUNT = 0x0200
    UAC_DONT_EXPIRE_PASSWORD = 0x10000

    def __init__(self, settings: Settings):
        self.settings = settings

    def _user_account_control(self, *, disabled: bool) -> int:
        value = self.UAC_NORMAL_ACCOUNT
        if disabled:
            value |= self.UAC_ACCOUNTDISABLE
        if self.settings.ad_password_never_expires:
            value |= self.UAC_DONT_EXPIRE_PASSWORD
        return value

    def _server(self) -> Server:
        tls = None
        if self.settings.ad_use_ssl:
            validate = ssl.CERT_REQUIRED if self.settings.ad_verify_tls else ssl.CERT_NONE
            tls = Tls(validate=validate, ca_certs_file=self.settings.ad_ca_cert_file or None)
        return Server(
            self.settings.ad_server,
            port=self.settings.ad_port,
            use_ssl=self.settings.ad_use_ssl,
            tls=tls,
            get_info=ALL,
            connect_timeout=10,
        )

    def _service_connection(self) -> Connection:
        if not self.settings.ad_server or not self.settings.ad_bind_dn:
            raise RuntimeError("Не заполнены настройки подключения к AD")
        conn = Connection(
            self._server(),
            user=self.settings.ad_bind_dn,
            password=self.settings.ad_bind_password,
            authentication=SIMPLE,
            auto_bind=True,
            receive_timeout=15,
        )
        return conn

    def test_connection(self) -> str:
        """Проверить LDAPS/LDAP-подключение и bind сервисной учетной записи."""
        with self._service_connection() as conn:
            if not conn.bound:
                raise RuntimeError("Подключение к AD установлено, но bind не выполнен")
        return "Подключение к Active Directory и bind выполнены успешно"

    def authenticate_operator(self, username: str, password: str) -> bool:
        """Проверить пароль доменного пользователя.

        Авторизация в приложении определяется таблицей domain_access_users.
        Здесь выполняется только проверка пароля и состояния учетной записи AD.

        Основной способ – SIMPLE bind по точному distinguishedName пользователя
        через LDAPS. Он не зависит от корректности NetBIOS-имени в AD_DOMAIN.
        NTLM оставлен запасным способом для совместимости.
        """
        if not self.settings.ad_login_enabled or not password:
            return False

        directory_user = self.get_user(username)
        if directory_user is None or not directory_user.is_enabled:
            return False

        attempts: list[tuple[str, str]] = [
            (SIMPLE, directory_user.distinguished_name),
        ]

        if self.settings.ad_domain:
            attempts.append(
                (NTLM, f"{self.settings.ad_domain}\\{directory_user.username}")
            )

        if self.settings.ad_upn_suffix:
            attempts.append(
                (SIMPLE, f"{directory_user.username}@{self.settings.ad_upn_suffix}")
            )

        for authentication, bind_user in attempts:
            conn = None
            try:
                conn = Connection(
                    self._server(),
                    user=bind_user,
                    password=password,
                    authentication=authentication,
                    auto_bind=True,
                    receive_timeout=15,
                )
                return True
            except LDAPException:
                continue
            finally:
                if conn is not None:
                    conn.unbind()

        return False

    @staticmethod
    def _entry_value(entry, attribute: str, default=""):
        value = getattr(entry, attribute, None)
        if value is None:
            return default
        return value.value if hasattr(value, "value") else default

    @classmethod
    def _entry_to_directory_user(cls, entry) -> ADDirectoryUser | None:
        username = str(cls._entry_value(entry, "sAMAccountName", "") or "").strip().lower()
        if not username:
            return None

        display_name = str(cls._entry_value(entry, "displayName", "") or "").strip()
        email = str(cls._entry_value(entry, "mail", "") or "").strip()
        user_account_control = int(cls._entry_value(entry, "userAccountControl", 0) or 0)
        raw_expires = cls._entry_value(entry, "accountExpires", 0)
        is_expired = False
        if isinstance(raw_expires, datetime):
            expires_at = raw_expires
            if expires_at.tzinfo is None:
                expires_at = expires_at.replace(tzinfo=timezone.utc)
            is_expired = expires_at.astimezone(timezone.utc) <= datetime.now(
                timezone.utc
            )
        else:
            try:
                filetime = int(raw_expires or 0)
            except (TypeError, ValueError):
                filetime = 0
            if filetime not in {0, 9223372036854775807}:
                windows_epoch = datetime(1601, 1, 1, tzinfo=timezone.utc)
                try:
                    expires_at = windows_epoch + timedelta(
                        microseconds=filetime // 10
                    )
                    is_expired = expires_at <= datetime.now(timezone.utc)
                except OverflowError:
                    is_expired = False

        raw_guid = cls._entry_value(entry, "objectGUID", "")
        if isinstance(raw_guid, bytes) and len(raw_guid) == 16:
            object_guid = str(uuid.UUID(bytes_le=raw_guid))
        else:
            object_guid = str(raw_guid or "").strip().strip("{}").lower()

        return ADDirectoryUser(
            username=username,
            display_name=display_name,
            email=email,
            distinguished_name=str(entry.entry_dn),
            is_enabled=(user_account_control & 2) == 0,
            object_guid=object_guid,
            is_expired=is_expired,
        )

    def search_users(self, query: str, limit: int = 20) -> list[ADDirectoryUser]:
        normalized_query = query.strip()
        if len(normalized_query) < 2:
            raise ValueError("Для поиска в AD введите не менее двух символов")

        safe = escape_filter_chars(normalized_query)
        search_filter = (
            "(&(objectCategory=person)(objectClass=user)"
            f"(|(sAMAccountName=*{safe}*)"
            f"(displayName=*{safe}*)"
            f"(sn=*{safe}*)"
            f"(givenName=*{safe}*)"
            f"(mail=*{safe}*)))"
        )

        with self._service_connection() as conn:
            conn.search(
                self.settings.ad_base_dn,
                search_filter,
                attributes=[
                    "sAMAccountName",
                    "displayName",
                    "mail",
                    "userAccountControl",
                    "distinguishedName",
                    "objectGUID",
                    "accountExpires",
                ],
                size_limit=max(1, min(limit, 50)),
            )
            users = [
                user
                for entry in conn.entries
                if (user := self._entry_to_directory_user(entry)) is not None
            ]

        return sorted(
            users,
            key=lambda item: (
                not item.is_enabled,
                item.display_name.lower(),
                item.username,
            ),
        )[:limit]

    def get_user(self, username: str) -> ADDirectoryUser | None:
        normalized = username.strip().lower()
        if not normalized:
            return None

        safe = escape_filter_chars(normalized)
        with self._service_connection() as conn:
            conn.search(
                self.settings.ad_base_dn,
                f"(&(objectCategory=person)(objectClass=user)(sAMAccountName={safe}))",
                attributes=[
                    "sAMAccountName",
                    "displayName",
                    "mail",
                    "userAccountControl",
                    "distinguishedName",
                    "objectGUID",
                    "accountExpires",
                ],
                size_limit=1,
            )
            if not conn.entries:
                return None
            return self._entry_to_directory_user(conn.entries[0])

    def users_by_email(self, email: str, limit: int = 10) -> list[ADDirectoryUser]:
        """Найти существующие AD-учетки по точному корпоративному e-mail.

        Проверяем как основной атрибут mail, так и proxyAddresses, потому что
        старые учетные записи могли заполняться по-разному.
        """
        normalized = str(email or "").strip().lower()
        if not normalized or "@" not in normalized:
            return []

        safe = escape_filter_chars(normalized)
        search_filter = (
            "(&(objectCategory=person)(objectClass=user)"
            f"(|(mail={safe})(proxyAddresses=smtp:{safe})))"
        )
        with self._service_connection() as conn:
            conn.search(
                self.settings.ad_base_dn,
                search_filter,
                attributes=[
                    "sAMAccountName",
                    "displayName",
                    "mail",
                    "userAccountControl",
                    "distinguishedName",
                    "objectGUID",
                    "accountExpires",
                ],
                size_limit=max(1, min(limit, 50)),
            )
            users = [
                user
                for entry in conn.entries
                if (user := self._entry_to_directory_user(entry)) is not None
            ]

        return sorted(
            users,
            key=lambda item: (
                not item.is_enabled,
                item.display_name.casefold(),
                item.username,
            ),
        )[:limit]

    def get_user_by_email(self, email: str) -> ADDirectoryUser | None:
        users = self.users_by_email(email, limit=1)
        return users[0] if users else None

    def logins_exist(self, logins: list[str]) -> set[str]:
        if not self.settings.ad_check_enabled:
            return set()

        normalized = list(dict.fromkeys(login.strip().lower() for login in logins if login.strip()))
        if not normalized:
            return set()

        alternatives = "".join(
            f"(sAMAccountName={escape_filter_chars(login)})"
            for login in normalized
        )
        search_filter = f"(&(objectClass=user)(|{alternatives}))"

        with self._service_connection() as conn:
            conn.search(
                self.settings.ad_base_dn,
                search_filter,
                attributes=["sAMAccountName"],
                size_limit=len(normalized),
            )
            return {
                str(entry.sAMAccountName.value).lower()
                for entry in conn.entries
                if getattr(entry, "sAMAccountName", None)
                and entry.sAMAccountName.value
            }

    def login_exists(self, login: str) -> bool:
        normalized = login.strip().lower()
        return normalized in self.logins_exist([normalized])

    def users_by_logins(self, logins: list[str]) -> dict[str, ADDirectoryUser]:
        """Получить карточки AD для набора логинов одним подключением."""
        if not self.settings.ad_check_enabled:
            return {}
        normalized = list(dict.fromkeys(login.strip().lower() for login in logins if login.strip()))
        if not normalized:
            return {}
        result: dict[str, ADDirectoryUser] = {}
        with self._service_connection() as conn:
            for offset in range(0, len(normalized), 200):
                chunk = normalized[offset:offset + 200]
                alternatives = "".join(f"(sAMAccountName={escape_filter_chars(login)})" for login in chunk)
                conn.search(self.settings.ad_base_dn, f"(&(objectCategory=person)(objectClass=user)(|{alternatives}))", attributes=["sAMAccountName","displayName","mail","userAccountControl","distinguishedName","objectGUID","accountExpires"], size_limit=len(chunk))
                for entry in conn.entries:
                    user = self._entry_to_directory_user(entry)
                    if user is not None:
                        result[user.username] = user
        return result

    def users_by_object_guids(
        self,
        object_guids: list[str],
    ) -> dict[str, ADDirectoryUser]:
        """Получить AD-пользователей по стабильным objectGUID пакетно."""
        if not self.settings.ad_check_enabled:
            return {}

        normalized: list[str] = []
        for value in object_guids:
            text = str(value or "").strip().strip("{}").lower()
            if not text:
                continue
            try:
                text = str(uuid.UUID(text))
            except ValueError:
                continue
            if text not in normalized:
                normalized.append(text)

        if not normalized:
            return {}

        result: dict[str, ADDirectoryUser] = {}
        with self._service_connection() as conn:
            for offset in range(0, len(normalized), 100):
                chunk = normalized[offset:offset + 100]
                clauses = []
                for guid_text in chunk:
                    guid_bytes = uuid.UUID(guid_text).bytes_le
                    clauses.append(f"(objectGUID={escape_bytes(guid_bytes)})")
                search_filter = (
                    "(&(objectCategory=person)(objectClass=user)"
                    f"(|{''.join(clauses)}))"
                )
                conn.search(
                    self.settings.ad_base_dn,
                    search_filter,
                    attributes=[
                        "sAMAccountName",
                        "displayName",
                        "mail",
                        "userAccountControl",
                        "distinguishedName",
                        "objectGUID",
                        "accountExpires",
                    ],
                    size_limit=len(chunk),
                )
                for entry in conn.entries:
                    user = self._entry_to_directory_user(entry)
                    if user is not None and user.object_guid:
                        result[user.object_guid.lower()] = user
        return result

    def get_user_by_object_guid(self, object_guid: str) -> ADDirectoryUser | None:
        normalized = str(object_guid or "").strip().strip("{}").lower()
        if not normalized:
            return None
        return self.users_by_object_guids([normalized]).get(normalized)

    def is_user_member_of_group(
        self,
        username: str,
        group_dn: str,
        *,
        object_guid: str = "",
    ) -> bool:
        """Проверить прямое членство пользователя в группе, ничего не меняя."""
        normalized_group = str(group_dn or "").strip()
        if not normalized_group:
            raise ValueError("Не передан DN группы AD")

        user = None
        if str(object_guid or "").strip():
            user = self.get_user_by_object_guid(object_guid)
        if user is None and str(username or "").strip():
            user = self.get_user(username)
        if user is None:
            raise RuntimeError(
                "Учетная запись AD не найдена перед проверкой группы"
            )

        safe_dn = escape_filter_chars(user.distinguished_name)
        with self._service_connection() as conn:
            conn.search(
                normalized_group,
                f"(&(objectClass=group)(member={safe_dn}))",
                search_scope=BASE,
                attributes=["distinguishedName"],
                size_limit=1,
            )
            return bool(conn.entries)

    def group_members(self, group_dn: str) -> list[ADDirectoryUser]:
        """Получить прямых пользователей группы одним сервисным bind.

        Чтение выполняется по DN из настроек. Вложенные группы намеренно не
        разворачиваются: сервис управляет только прямым составом группы
        доступа Техэксперта.
        """

        normalized_group = str(group_dn or "").strip()
        if not normalized_group:
            raise ValueError("Не передан DN группы AD")

        users: list[ADDirectoryUser] = []
        with self._service_connection() as conn:
            conn.search(
                normalized_group,
                "(objectClass=group)",
                search_scope=BASE,
                attributes=["member"],
                size_limit=1,
            )
            if not conn.entries:
                raise RuntimeError("Группа Техэксперта не найдена в AD")

            member_attribute = getattr(conn.entries[0], "member", None)
            member_dns = list(getattr(member_attribute, "values", []) or [])
            for member_dn in member_dns:
                conn.search(
                    str(member_dn),
                    "(&(objectCategory=person)(objectClass=user))",
                    search_scope=BASE,
                    attributes=[
                        "sAMAccountName",
                        "displayName",
                        "mail",
                        "userAccountControl",
                        "distinguishedName",
                        "objectGUID",
                        "accountExpires",
                    ],
                    size_limit=1,
                )
                if not conn.entries:
                    continue
                user = self._entry_to_directory_user(conn.entries[0])
                if user is not None:
                    users.append(user)

        return sorted(
            users,
            key=lambda item: (
                item.display_name.casefold(),
                item.username,
            ),
        )

    def _group_user(
        self,
        username: str,
        object_guid: str,
    ) -> ADDirectoryUser:
        user = None
        if str(object_guid or "").strip():
            user = self.get_user_by_object_guid(object_guid)
        if user is None and str(username or "").strip():
            user = self.get_user(username)
        if user is None:
            raise RuntimeError("Учетная запись AD не найдена")
        if not user.distinguished_name:
            raise RuntimeError("AD не вернул DN учетной записи")
        return user

    def ensure_user_in_group(
        self,
        username: str,
        group_dn: str,
        *,
        object_guid: str = "",
    ) -> str:
        """Идемпотентно добавить пользователя в группу без перемещения OU."""

        normalized_group = str(group_dn or "").strip()
        if not normalized_group:
            raise ValueError("Не передан DN группы AD")
        user = self._group_user(username, object_guid)
        if self.settings.dry_run:
            return "dry_run"

        safe_dn = escape_filter_chars(user.distinguished_name)
        with self._service_connection() as conn:
            conn.search(
                normalized_group,
                f"(&(objectClass=group)(member={safe_dn}))",
                search_scope=BASE,
                attributes=["distinguishedName"],
                size_limit=1,
            )
            if conn.entries:
                return "already_member"
            if not conn.modify(
                normalized_group,
                {"member": [(MODIFY_ADD, [user.distinguished_name])]},
            ):
                raise RuntimeError(
                    "Не удалось добавить пользователя в группу Техэксперта: "
                    f"{conn.result.get('message') or conn.result}"
                )
        return "added"

    def remove_user_from_group(
        self,
        username: str,
        group_dn: str,
        *,
        object_guid: str = "",
    ) -> str:
        """Идемпотентно удалить прямое членство, не меняя саму учетку."""

        normalized_group = str(group_dn or "").strip()
        if not normalized_group:
            raise ValueError("Не передан DN группы AD")
        user = self._group_user(username, object_guid)
        if self.settings.dry_run:
            return "dry_run"

        safe_dn = escape_filter_chars(user.distinguished_name)
        with self._service_connection() as conn:
            conn.search(
                normalized_group,
                f"(&(objectClass=group)(member={safe_dn}))",
                search_scope=BASE,
                attributes=["distinguishedName"],
                size_limit=1,
            )
            if not conn.entries:
                return "not_member"
            if not conn.modify(
                normalized_group,
                {"member": [(MODIFY_DELETE, [user.distinguished_name])]},
            ):
                raise RuntimeError(
                    "Не удалось удалить пользователя из группы Техэксперта: "
                    f"{conn.result.get('message') or conn.result}"
                )
        return "removed"

    def test_group(self, group_dn: str) -> str:
        """Проверить существование маркерной группы без изменения AD."""
        normalized_group = str(group_dn or "").strip()
        if not normalized_group:
            raise ValueError("Не передан DN группы AD")
        with self._service_connection() as conn:
            conn.search(
                normalized_group,
                "(objectClass=group)",
                search_scope=BASE,
                attributes=["distinguishedName"],
                size_limit=1,
            )
            if not conn.entries:
                raise RuntimeError("Маркерная группа Техэксперта не найдена в AD")
        return "Группа AD найдена; SMTP-подключение также доступно"


    def create_disabled_user(
        self,
        login: str,
        password_candidates: list[str],
        last_name: str,
        first_name: str,
        middle_name: str,
        corporate_email: str,
    ) -> ADCreateResult:
        upn = f"{login}@{self.settings.ad_upn_suffix}"
        display_name = " ".join(
            part for part in [last_name, first_name, middle_name] if part
        )

        # Поле «Полное имя» / Name в оснастке AD формируется из RDN (CN),
        # поэтому основным CN должно быть ФИО, а не sAMAccountName.
        primary_cn = display_name
        dn = f"CN={escape_rdn(primary_cn)},{self.settings.ad_users_ou}"

        if not password_candidates:
            raise ValueError("Не переданы варианты пароля AD")
        if self.settings.dry_run:
            return ADCreateResult(
                dn=dn,
                login=login,
                upn=upn,
                accepted_password=password_candidates[0],
            )

        attributes = {
            "objectClass": ["top", "person", "organizationalPerson", "user"],
            "cn": primary_cn,
            "displayName": display_name,
            "givenName": first_name,
            "sn": last_name,
            "sAMAccountName": login,
            "userPrincipalName": upn,
            "mail": corporate_email,
            "userAccountControl": self._user_account_control(disabled=True),
        }
        with self._service_connection() as conn:
            if not conn.add(dn, attributes=attributes):
                # CN должен быть уникален внутри одной OU. Для полного тезки
                # сохраняем чистое ФИО в displayName, а в CN добавляем логин.
                if int(conn.result.get("result") or 0) == 68:
                    fallback_cn = f"{display_name} ({login})"
                    dn = (
                        f"CN={escape_rdn(fallback_cn)},"
                        f"{self.settings.ad_users_ou}"
                    )
                    fallback_attributes = {
                        **attributes,
                        "cn": fallback_cn,
                    }
                    if not conn.add(dn, attributes=fallback_attributes):
                        raise RuntimeError(
                            "AD не создал пользователя с резервным полным "
                            f"именем: {conn.result.get('message') or conn.result}"
                        )
                else:
                    raise RuntimeError(
                        "AD не создал пользователя: "
                        f"{conn.result.get('message') or conn.result}"
                    )
            accepted_password = ""
            last_password_error = ""
            for candidate in password_candidates:
                if conn.extend.microsoft.modify_password(dn, candidate):
                    accepted_password = candidate
                    break
                last_password_error = str(conn.result.get("message") or conn.result)
            if not accepted_password:
                # Не оставляем объект без рабочего пароля.
                conn.delete(dn)
                raise RuntimeError(f"AD не принял ни один сгенерированный пароль: {last_password_error}")
            # Параметры «Срок действия пароля не ограничен» и
            # «Требовать смену пароля при следующем входе» взаимоисключающие.
            # Для проекта приоритет имеет бессрочный пароль.
            if (
                self.settings.ad_force_change_at_first_logon
                and not self.settings.ad_password_never_expires
            ):
                if not conn.modify(dn, {"pwdLastSet": [(MODIFY_REPLACE, [0])]}):
                    raise RuntimeError(f"AD не установил смену пароля при первом входе: {conn.result}")
            for group_dn in self.settings.ad_default_group_dns:
                if not conn.modify(group_dn, {"member": [(MODIFY_ADD, [dn])]}):
                    raise RuntimeError(f"Не удалось добавить пользователя в группу {group_dn}: {conn.result}")
        return ADCreateResult(dn=dn, login=login, upn=upn, accepted_password=accepted_password)

    def enable_user(self, dn: str) -> None:
        if self.settings.dry_run:
            return
        with self._service_connection() as conn:
            user_account_control = self._user_account_control(disabled=False)
            if not conn.modify(
                dn,
                {"userAccountControl": [(MODIFY_REPLACE, [user_account_control])]},
            ):
                raise RuntimeError(f"AD не включил пользователя: {conn.result.get('message') or conn.result}")

    def enable_existing_user(self, dn: str) -> None:
        """Включить существующую учетку, сохранив остальные флаги UAC."""

        normalized_dn = str(dn or "").strip()
        if not normalized_dn:
            raise ValueError("Не передан DN учетной записи AD")
        if self.settings.dry_run:
            return
        with self._service_connection() as conn:
            conn.search(
                normalized_dn,
                "(objectClass=user)",
                search_scope=BASE,
                attributes=["userAccountControl"],
                size_limit=1,
            )
            if not conn.entries:
                raise RuntimeError("Учетная запись AD не найдена")
            current = int(
                self._entry_value(
                    conn.entries[0],
                    "userAccountControl",
                    0,
                )
                or 0
            )
            if not current & self.UAC_ACCOUNTDISABLE:
                return
            enabled = current & ~self.UAC_ACCOUNTDISABLE
            if not conn.modify(
                normalized_dn,
                {"userAccountControl": [(MODIFY_REPLACE, [enabled])]},
            ):
                raise RuntimeError(
                    "AD не включил пользователя: "
                    f"{conn.result.get('message') or conn.result}"
                )

    def reactivate_existing_user(self, dn: str) -> None:
        """Включить прежнюю учетку и снять оставшийся срок увольнения."""

        normalized_dn = str(dn or "").strip()
        if not normalized_dn:
            raise ValueError("Не передан DN учетной записи AD")
        self.enable_existing_user(normalized_dn)
        if self.settings.dry_run:
            return
        with self._service_connection() as conn:
            if not conn.modify(
                normalized_dn,
                {"accountExpires": [(MODIFY_REPLACE, [0])]},
            ):
                raise RuntimeError(
                    "AD не снял срок действия учетной записи: "
                    f"{conn.result.get('message') or conn.result}"
                )

    def delete_user(self, dn: str) -> None:
        if self.settings.dry_run:
            return
        with self._service_connection() as conn:
            if not conn.delete(dn):
                raise RuntimeError(f"Не удалось удалить заготовку AD: {conn.result}")

    def disable_user(self, login: str) -> None:
        """Немедленно отключить существующую AD-учетку, сохранив UAC-флаги."""
        normalized = str(login or "").strip().lower()
        if not normalized:
            raise ValueError("Не передан логин AD")
        if self.settings.dry_run:
            return

        safe_login = escape_filter_chars(normalized)
        with self._service_connection() as conn:
            conn.search(
                self.settings.ad_base_dn,
                f"(&(objectCategory=person)(objectClass=user)(sAMAccountName={safe_login}))",
                attributes=["userAccountControl"],
                size_limit=1,
            )
            if not conn.entries:
                raise RuntimeError("Учетная запись AD не найдена")
            entry = conn.entries[0]
            current = int(
                self._entry_value(entry, "userAccountControl", 0) or 0
            )
            if current & self.UAC_ACCOUNTDISABLE:
                return
            disabled = current | self.UAC_ACCOUNTDISABLE
            if not conn.modify(
                str(entry.entry_dn),
                {"userAccountControl": [(MODIFY_REPLACE, [disabled])]},
            ):
                raise RuntimeError(
                    "AD не отключил пользователя: "
                    f"{conn.result.get('message') or conn.result}"
                )

    def set_account_expiration(self, login: str, dismissal_date: date) -> None:
        """Expire at 00:00 on the day after the employee's last working date."""
        if self.settings.dry_run:
            return
        safe_login = escape_filter_chars(login)
        with self._service_connection() as conn:
            conn.search(
                self.settings.ad_base_dn,
                f"(&(objectClass=user)(sAMAccountName={safe_login}))",
                attributes=["distinguishedName"],
                size_limit=1,
            )
            if not conn.entries:
                raise RuntimeError("Учетная запись AD не найдена")
            dn = str(conn.entries[0].entry_dn)
            account_expires = self._date_to_filetime(dismissal_date)
            if not conn.modify(dn, {"accountExpires": [(MODIFY_REPLACE, [account_expires])]}):
                raise RuntimeError(f"AD не установил срок действия: {conn.result}")

    def _date_to_filetime(self, last_working_date: date) -> int:
        local_tz = ZoneInfo(self.settings.app_timezone)
        local_expiration = datetime.combine(last_working_date + timedelta(days=1), time.min, tzinfo=local_tz)
        utc_expiration = local_expiration.astimezone(timezone.utc)
        windows_epoch = datetime(1601, 1, 1, tzinfo=timezone.utc)
        return int((utc_expiration - windows_epoch).total_seconds() * 10_000_000)
