from __future__ import annotations

from datetime import date, datetime, timezone

from sqlalchemy import Boolean, Date, DateTime, Integer, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from app.db import Base


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


class PreliminaryDismissalSettings(Base):
    """Независимое правило входящей почты для одной кадровой организации."""

    __tablename__ = "preliminary_dismissal_settings"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    enabled: Mapped[bool] = mapped_column(Boolean, default=False, index=True)
    source_id: Mapped[str] = mapped_column(String(128), default="", index=True)
    imap_host: Mapped[str] = mapped_column(String(512), default="")
    imap_port: Mapped[int] = mapped_column(Integer, default=993)
    imap_ssl: Mapped[bool] = mapped_column(Boolean, default=True)
    imap_username: Mapped[str] = mapped_column(String(512), default="")
    imap_password_encrypted: Mapped[str] = mapped_column(Text, default="")
    imap_folder: Mapped[str] = mapped_column(String(512), default="INBOX")
    imap_lookback_days: Mapped[int] = mapped_column(Integer, default=7)
    sender_filter: Mapped[str] = mapped_column(String(512), default="")
    subject_filter: Mapped[str] = mapped_column(String(512), default="")
    config_key: Mapped[str] = mapped_column(String(64), default="", index=True)
    last_scanned_uid: Mapped[str] = mapped_column(String(128), default="")
    last_status: Mapped[str] = mapped_column(String(32), default="never", index=True)
    last_error: Mapped[str] = mapped_column(Text, default="")
    last_checked_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    last_success_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    updated_by: Mapped[str] = mapped_column(String(256), default="")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, onupdate=utcnow
    )


class PreliminaryDismissalSourceRule(Base):
    """Один доверенный отправитель и его условие по теме письма."""

    __tablename__ = "preliminary_dismissal_source_rules"
    __table_args__ = (
        UniqueConstraint(
            "settings_id",
            "sender_email",
            "subject_mode",
            "subject_value",
            name="uq_preliminary_source_rule_match",
        ),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    settings_id: Mapped[int] = mapped_column(Integer, index=True)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True, index=True)
    label: Mapped[str] = mapped_column(String(256), default="")
    sender_email: Mapped[str] = mapped_column(String(320), index=True)
    subject_mode: Mapped[str] = mapped_column(String(16), default="contains")
    subject_value: Mapped[str] = mapped_column(String(512))
    updated_by: Mapped[str] = mapped_column(String(256), default="")
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, onupdate=utcnow
    )


class PreliminaryDismissalMessage(Base):
    """Обработанное входящее письмо; Message-ID/хеш не дают прочитать его дважды."""

    __tablename__ = "preliminary_dismissal_messages"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    message_key: Mapped[str] = mapped_column(String(128), unique=True, index=True)
    source_id: Mapped[str] = mapped_column(String(128), default="", index=True)
    source_rule_id: Mapped[int] = mapped_column(Integer, default=0, index=True)
    imap_uid: Mapped[str] = mapped_column(String(128), default="", index=True)
    message_id: Mapped[str] = mapped_column(String(1024), default="")
    message_date: Mapped[str] = mapped_column(String(256), default="")
    sender: Mapped[str] = mapped_column(String(512), default="")
    subject: Mapped[str] = mapped_column(String(1024), default="")
    body_hash: Mapped[str] = mapped_column(String(64), default="", index=True)
    status: Mapped[str] = mapped_column(String(32), default="processed", index=True)
    items_count: Mapped[int] = mapped_column(Integer, default=0)
    matched_count: Mapped[int] = mapped_column(Integer, default=0)
    error: Mapped[str] = mapped_column(Text, default="")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class PreliminaryDismissalItem(Base):
    """Один эпизод предварительного уведомления по работнику одной организации."""

    __tablename__ = "preliminary_dismissal_items"
    __table_args__ = (
        UniqueConstraint(
            "source_id",
            "normalized_fio",
            "sequence",
            name="uq_preliminary_dismissal_source_fio_sequence",
        ),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    source_id: Mapped[str] = mapped_column(String(128), index=True)
    source_name: Mapped[str] = mapped_column(String(256), default="")
    sequence: Mapped[int] = mapped_column(Integer, default=1)
    worker_key: Mapped[str] = mapped_column(String(64), default="", index=True)
    normalized_fio: Mapped[str] = mapped_column(String(512), index=True)
    fio: Mapped[str] = mapped_column(String(512), index=True)
    dismissal_date: Mapped[date] = mapped_column(Date, index=True)
    position: Mapped[str] = mapped_column(String(512), default="")
    departments_json: Mapped[str] = mapped_column(Text, default="[]")
    status: Mapped[str] = mapped_column(String(32), default="active", index=True)
    match_error: Mapped[str] = mapped_column(Text, default="")
    latest_message_key: Mapped[str] = mapped_column(String(128), default="", index=True)
    latest_message_uid: Mapped[str] = mapped_column(String(128), default="")
    equipment_notice_id: Mapped[int | None] = mapped_column(
        Integer, nullable=True, index=True
    )
    first_notified_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow
    )
    latest_notified_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow
    )
    confirmed_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    expired_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, onupdate=utcnow
    )
