from __future__ import annotations

from collections.abc import Generator

from sqlalchemy import create_engine, inspect, text
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker

from app.config import get_settings


class Base(DeclarativeBase):
    pass


settings = get_settings()
connect_args = {"check_same_thread": False} if settings.database_url.startswith("sqlite") else {}
engine = create_engine(settings.database_url, connect_args=connect_args, pool_pre_ping=True)
SessionLocal = sessionmaker(bind=engine, autoflush=False, autocommit=False, expire_on_commit=False)


def get_db() -> Generator[Session, None, None]:
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


def ensure_compatibility_schema() -> None:
    """Добавляет совместимые поля в существующую локальную БД без ее сброса."""
    inspector = inspect(engine)
    tables = set(inspector.get_table_names())

    def add_missing_columns(
        connection,
        table: str,
        definitions: dict[str, str],
    ) -> None:
        if table not in tables:
            return
        existing = {column["name"] for column in inspector.get_columns(table)}
        for name, definition in definitions.items():
            if name in existing:
                continue
            connection.execute(
                text(f"ALTER TABLE {table} ADD COLUMN {name} {definition}")
            )

    with engine.begin() as connection:
        add_missing_columns(
            connection,
            "synology_control_settings",
            {
                "max_disables_per_run": "INTEGER NOT NULL DEFAULT 10",
                "mass_disable_ack_at": "DATETIME",
                "mass_disable_ack_count": "INTEGER NOT NULL DEFAULT 0",
                "mass_disable_ack_by": "VARCHAR(256) NOT NULL DEFAULT ''",
                "block_window_date": "DATE",
                "block_attempts": "INTEGER NOT NULL DEFAULT 0",
                "last_block_attempt_at": "DATETIME",
            },
        )
        add_missing_columns(
            connection,
            "synology_account_states",
            {
                "disabled_reason_code": "VARCHAR(64) NOT NULL DEFAULT ''",
                "attention_state": "VARCHAR(64) NOT NULL DEFAULT ''",
                "attention_details": "TEXT NOT NULL DEFAULT ''",
                "attention_at": "DATETIME",
            },
        )
        add_missing_columns(
            connection,
            "synology_sync_runs",
            {
                "disabled_accounts": "INTEGER NOT NULL DEFAULT 0",
                "guard_message": "TEXT NOT NULL DEFAULT ''",
            },
        )
        add_missing_columns(
            connection,
            "dismissal_equipment_notices",
            {"event_ids_json": "TEXT NOT NULL DEFAULT '[]'"},
        )
        add_missing_columns(
            connection,
            "zimbra_lifecycle_settings",
            {
                "allow_employment_close": "BOOLEAN NOT NULL DEFAULT 0",
                "allow_alias_remove": "BOOLEAN NOT NULL DEFAULT 0",
            },
        )
        add_missing_columns(
            connection,
            "ad_reactivation_alerts",
            {
                "block_run_id": "INTEGER NOT NULL DEFAULT 0",
                "dismissal_date": "DATE",
                "resolution": "VARCHAR(32) NOT NULL DEFAULT ''",
                "resolved_by": "VARCHAR(256) NOT NULL DEFAULT ''",
                "resolved_at": "DATETIME",
                "last_error": "TEXT NOT NULL DEFAULT ''",
                "candidates_json": "TEXT NOT NULL DEFAULT '[]'",
                "last_checked_at": "DATETIME",
            },
        )
        add_missing_columns(
            connection,
            "preliminary_dismissal_settings",
            {
                "imap_host": "VARCHAR(512) NOT NULL DEFAULT ''",
                "imap_port": "INTEGER NOT NULL DEFAULT 993",
                "imap_ssl": "BOOLEAN NOT NULL DEFAULT 1",
                "imap_username": "VARCHAR(512) NOT NULL DEFAULT ''",
                "imap_password_encrypted": "TEXT NOT NULL DEFAULT ''",
                "imap_lookback_days": "INTEGER NOT NULL DEFAULT 7",
            },
        )
        add_missing_columns(
            connection,
            "preliminary_dismissal_messages",
            {
                "source_id": "VARCHAR(128) NOT NULL DEFAULT ''",
                "source_rule_id": "INTEGER NOT NULL DEFAULT 0",
            },
        )
        add_missing_columns(
            connection,
            "zimbra_mail_cleanup_runs",
            {
                "processed_mailboxes": "INTEGER NOT NULL DEFAULT 0",
                "progress_at": "DATETIME",
            },
        )

        if "hr_source_records" in tables:
            add_missing_columns(
                connection,
                "hr_source_records",
                {
                    "personal_email": (
                        "VARCHAR(320) NOT NULL DEFAULT ''"
                    ),
                    "mobile_phone": (
                        "VARCHAR(128) NOT NULL DEFAULT ''"
                    ),
                    "techexpert_access": (
                        "BOOLEAN NOT NULL DEFAULT 0"
                    ),
                },
            )

        add_missing_columns(
            connection,
            "techexpert_settings",
            {
                "registration_subject": (
                    "VARCHAR(512) NOT NULL DEFAULT ''"
                ),
                "registration_body_html": "TEXT NOT NULL DEFAULT ''",
                "recovery_subject": (
                    "VARCHAR(512) NOT NULL DEFAULT ''"
                ),
                "recovery_body_html": "TEXT NOT NULL DEFAULT ''",
            },
        )
        add_missing_columns(
            connection,
            "techexpert_registration_requests",
            {
                "request_kind": (
                    "VARCHAR(32) NOT NULL DEFAULT 'registration'"
                ),
                "queued_at": "DATETIME",
                "scheduled_for": "DATETIME",
                "next_attempt_at": "DATETIME",
            },
        )
        add_missing_columns(
            connection,
            "techexpert_notifications",
            {
                "department": "VARCHAR(512) NOT NULL DEFAULT ''",
                "group_removal_status": (
                    "VARCHAR(32) NOT NULL DEFAULT 'not_started'"
                ),
                "group_removed_at": "DATETIME",
                "group_removal_error": "TEXT NOT NULL DEFAULT ''",
            },
        )
        add_missing_columns(
            connection,
            "techexpert_notification_batch_items",
            {"department": "VARCHAR(512) NOT NULL DEFAULT ''"},
        )
        add_missing_columns(
            connection,
            "techexpert_actualization_items",
            {
                "source_login": "VARCHAR(256) NOT NULL DEFAULT ''",
                "source_password_encrypted": "TEXT NOT NULL DEFAULT ''",
            },
        )

        if "onec_additional_sources" in tables:
            columns = {
                column["name"]
                for column in inspector.get_columns("onec_additional_sources")
            }
            folder_added = "imap_folder" not in columns
            if folder_added:
                connection.execute(
                    text(
                        "ALTER TABLE onec_additional_sources "
                        "ADD COLUMN imap_folder VARCHAR(512) NOT NULL DEFAULT 'INBOX'"
                    )
                )
            if "is_primary" not in columns:
                connection.execute(
                    text(
                        "ALTER TABLE onec_additional_sources "
                        "ADD COLUMN is_primary BOOLEAN NOT NULL DEFAULT 0"
                    )
                )
            if folder_added:
                connection.execute(
                    text(
                        "UPDATE onec_additional_sources "
                        "SET imap_folder = :folder"
                    ),
                    {"folder": settings.onec_imap_folder.strip() or "INBOX"},
                )
