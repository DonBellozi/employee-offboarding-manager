from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import Boolean, DateTime, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.db import Base


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


class ZimbraMailCleanupSettings(Base):
    """Расписание централизованной очистки служебных писем."""

    __tablename__ = "zimbra_mail_cleanup_settings"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, default=1)
    schedule_mode: Mapped[str] = mapped_column(
        String(32), default="manual"
    )
    schedule_weekday: Mapped[int] = mapped_column(Integer, default=6)
    schedule_time: Mapped[str] = mapped_column(String(5), default="03:00")
    updated_by: Mapped[str] = mapped_column(String(256), default="")
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, onupdate=utcnow
    )


class ZimbraMailRetentionRule(Base):
    """Одно безопасно ограниченное правило хранения сообщений."""

    __tablename__ = "zimbra_mail_retention_rules"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(String(256))
    condition_type: Mapped[str] = mapped_column(String(16), index=True)
    condition_value: Mapped[str] = mapped_column(String(320), index=True)
    retention_days: Mapped[int] = mapped_column(Integer)
    enabled: Mapped[bool] = mapped_column(Boolean, default=False, index=True)
    automatic_cleanup: Mapped[bool] = mapped_column(
        Boolean, default=False, index=True
    )
    scope_mode: Mapped[str] = mapped_column(String(32), default="all")
    mailboxes_json: Mapped[str] = mapped_column(Text, default="[]")
    last_run_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    last_run_status: Mapped[str] = mapped_column(String(32), default="")
    created_by: Mapped[str] = mapped_column(String(256), default="")
    updated_by: Mapped[str] = mapped_column(String(256), default="")
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, onupdate=utcnow
    )
    deleted_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True, index=True
    )


class ZimbraMailCleanupRun(Base):
    """Итог проверки или очистки одного правила."""

    __tablename__ = "zimbra_mail_cleanup_runs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    rule_id: Mapped[int] = mapped_column(Integer, index=True)
    rule_name: Mapped[str] = mapped_column(String(256), default="")
    mode: Mapped[str] = mapped_column(String(32), index=True)
    trigger: Mapped[str] = mapped_column(String(32), default="manual", index=True)
    status: Mapped[str] = mapped_column(String(32), default="running", index=True)
    initiated_by: Mapped[str] = mapped_column(String(256), default="")
    source_preview_run_id: Mapped[int] = mapped_column(Integer, default=0)
    checked_mailboxes: Mapped[int] = mapped_column(Integer, default=0)
    matched_mailboxes: Mapped[int] = mapped_column(Integer, default=0)
    found_messages: Mapped[int] = mapped_column(Integer, default=0)
    deleted_messages: Mapped[int] = mapped_column(Integer, default=0)
    truncated_mailboxes: Mapped[int] = mapped_column(Integer, default=0)
    error_count: Mapped[int] = mapped_column(Integer, default=0)
    duration_ms: Mapped[int] = mapped_column(Integer, default=0)
    details_json: Mapped[str] = mapped_column(Text, default="[]")
    rule_snapshot_json: Mapped[str] = mapped_column(Text, default="{}")
    error_message: Mapped[str] = mapped_column(Text, default="")
    started_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, index=True
    )
    completed_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
