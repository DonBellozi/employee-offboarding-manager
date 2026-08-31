from __future__ import annotations

from functools import lru_cache
from pathlib import Path
from typing import Annotated, Literal

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    app_name: str = "Управление жизненным циклом учетных записей"
    app_secret_key: str = "change-me-to-a-long-secret"
    app_base_url: str = "http://localhost:8000"
    app_timezone: str = "Europe/Moscow"
    database_url: str = "sqlite:///./data/app.db"

    # Глобальный безопасный режим снят при переходе проекта в опытную
    # эксплуатацию. Свойство оставлено только как совместимость со старыми
    # модулями: оно всегда False и не настраивается через окружение.
    @property
    def dry_run(self) -> bool:
        return False

    # Для HTTP оставляем false. После перехода на HTTPS меняем на true.
    session_cookie_secure: bool = False
    session_cookie_samesite: Literal["lax", "strict", "none"] = "lax"
    session_cookie_name: str = "employee_offboarding_manager_session"

    bootstrap_admin_username: str = "admin"
    bootstrap_admin_password: str = "ChangeMeNow!123"

    auth_mode: Literal["local", "hybrid", "ad"] = "hybrid"
    ad_login_enabled: bool = False
    ad_allowed_group_dn: str = ""

    ad_check_enabled: bool = True
    ad_server: str = ""
    ad_port: int = 636
    ad_use_ssl: bool = True
    ad_verify_tls: bool = True
    ad_ca_cert_file: str = ""
    ad_domain: str = ""
    ad_base_dn: str = ""
    ad_users_ou: str = ""
    ad_bind_dn: str = ""
    ad_bind_password: str = ""
    ad_upn_suffix: str = ""
    ad_force_change_at_first_logon: bool = False
    ad_password_never_expires: bool = True
    ad_default_group_dns: Annotated[list[str], NoDecode] = Field(default_factory=list)

    zimbra_check_enabled: bool = False
    zimbra_backend: Literal["ssh_zmprov", "disabled"] = "ssh_zmprov"
    zimbra_ssh_host: str = ""
    zimbra_ssh_port: int = 22
    zimbra_ssh_user: str = "provisioner"
    zimbra_ssh_auth: Literal["key", "password", "auto"] = "key"
    zimbra_ssh_private_key: str = "/run/secrets/zimbra_ssh_key"
    zimbra_ssh_password: str = ""
    zimbra_ssh_password_file: str = ""
    zimbra_ssh_known_hosts: str = "/app/known_hosts"
    zimbra_domains: Annotated[list[str], NoDecode] = Field(default_factory=list)
    zimbra_domain_mode: Literal["separate", "primary_alias"] = "separate"
    zimbra_primary_domain: str = ""
    zimbra_create_aliases: bool = True
    zimbra_cos_id: str = ""
    zimbra_mail_cleanup_workers: int = Field(default=4, ge=1, le=8)

    smtp_host: str = ""
    smtp_port: int = 587
    smtp_starttls: bool = True
    smtp_ssl: bool = False
    smtp_username: str = ""
    smtp_password: str = ""
    smtp_from: str = ""
    smtp_timeout_seconds: int = 20
    smtp_retry_attempts: int = 3
    smtp_retry_delay_seconds: float = 2.0

    # IT Invent / MS SQL Server. Интеграция намеренно read-only:
    # приложение выполняет только SELECT, а учетной записи SQL следует
    # предоставить только права чтения на БД ITInvent.
    itinvent_enabled: bool = False
    itinvent_db_host: str = ""
    itinvent_db_port: int = 1433
    itinvent_db_name: str = "ITInvent"
    itinvent_db_username: str = ""
    itinvent_db_password: str = ""
    itinvent_connect_timeout_seconds: int = 5
    itinvent_query_timeout_seconds: int = 10
    itinvent_issued_location_no: int = 24

    # Synology DSM. Первый этап интеграции намеренно read-only: приложение
    # получает локальные учетные записи по SSH/synouser, классифицирует их и
    # рассчитывает желаемые lifecycle-действия, но не меняет DSM.
    synology_enabled: bool = False
    synology_ssh_host: str = ""
    synology_ssh_port: int = 22
    synology_ssh_user: str = "provisioner"
    synology_ssh_auth: Literal["key", "password", "auto"] = "auto"
    synology_ssh_private_key: str = "/run/secrets/synology_ssh_key"
    synology_ssh_password: str = ""
    synology_ssh_password_file: str = ""
    synology_ssh_known_hosts: str = "/app/known_hosts"
    synology_ssh_use_sudo: bool = True
    synology_synouser_command: str = "synouser"
    synology_connect_timeout_seconds: int = 10
    synology_command_timeout_seconds: int = 20

    # Получение кадровой выгрузки 1С. Настройки задаются через
    # окружение/Portainer; Web показывает безопасную часть и запускает
    # проверки/предварительный анализ без изменения внешних систем.
    onec_imap_host: str = ""
    onec_imap_port: int = 993
    onec_imap_ssl: bool = True
    onec_imap_username: str = ""
    onec_imap_password: str = ""
    onec_imap_folder: str = "INBOX"
    onec_imap_from_contains: str = "1c-robot@"
    onec_imap_lookback_days: int = 3
    onec_attachment_filename: str = "Штатные сотрудники - Телефонный справочник2 (XLSX).xlsx"
    onec_header_search_rows: int = 20
    onec_data_dir: str = "/app/data/onec"
    onec_source_domain: str = ""

    # Автоматический импорт кадровой выгрузки. При запуске приложения
    # выполняется catch-up: новый файл будет обработан сразу, даже если
    # сервер был выключен в плановое время.
    onec_auto_import_enabled: bool = True
    onec_auto_import_time: str = "09:00"
    onec_auto_import_startup_catchup: bool = True

    # Отдельный HMAC-секрет можно задать позже. Пока при пустом значении
    # используется APP_SECRET_KEY, чтобы импорт можно было запустить сразу.
    onec_worker_hash_secret: str = ""

    mail_password_length: int = 16
    mail_password_specials: str = "!@#$%&?"
    ad_password_min_length: int = 8
    ad_password_max_length: int = 12
    ad_password_specials: str = "!@#$%&?"

    rollback_ad_on_zimbra_failure: bool = False

    @field_validator("zimbra_domains", mode="before")
    @classmethod
    def split_domains(cls, value: object) -> object:
        if isinstance(value, str):
            return [item.strip() for item in value.split(",") if item.strip()]
        return value

    @field_validator("ad_default_group_dns", mode="before")
    @classmethod
    def split_group_dns(cls, value: object) -> object:
        if isinstance(value, str):
            return [item.strip() for item in value.split(";") if item.strip()]
        return value

    @field_validator("onec_auto_import_time")
    @classmethod
    def validate_onec_auto_import_time(cls, value: str) -> str:
        text = str(value or "").strip()
        match = __import__("re").fullmatch(r"(\d{2}):(\d{2})", text)
        if not match:
            raise ValueError(
                "ONEC_AUTO_IMPORT_TIME должен быть в формате HH:MM"
            )
        hour, minute = (int(part) for part in match.groups())
        if hour > 23 or minute > 59:
            raise ValueError(
                "ONEC_AUTO_IMPORT_TIME содержит недопустимое время"
            )
        return f"{hour:02d}:{minute:02d}"

    @field_validator("app_secret_key")
    @classmethod
    def validate_secret(cls, value: str) -> str:
        if len(value) < 16:
            raise ValueError("APP_SECRET_KEY должен быть не короче 16 символов")
        return value

    @field_validator("session_cookie_name")
    @classmethod
    def validate_cookie_name(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("SESSION_COOKIE_NAME не может быть пустым")
        return value

    def ensure_runtime_directories(self) -> None:
        if self.database_url.startswith("sqlite:///"):
            path = Path(self.database_url.removeprefix("sqlite:///"))
            path.parent.mkdir(parents=True, exist_ok=True)
        Path(self.onec_data_dir).mkdir(parents=True, exist_ok=True)


@lru_cache
def get_settings() -> Settings:
    settings = Settings()
    settings.ensure_runtime_directories()
    return settings
