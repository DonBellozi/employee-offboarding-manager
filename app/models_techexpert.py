from __future__ import annotations

from datetime import date, datetime, timezone

from sqlalchemy import Boolean, Date, DateTime, Integer, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from app.db import Base


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


class TechExpertSettings(Base):
    """Настройки уведомительного контура, управляемые из Web."""

    __tablename__ = "techexpert_settings"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, default=1)
    enabled: Mapped[bool] = mapped_column(Boolean, default=False)
    source_domain: Mapped[str] = mapped_column(String(255), default="")
    ad_group_dn: Mapped[str] = mapped_column(String(1024), default="")
    recipient_email: Mapped[str] = mapped_column(String(320), default="")
    notification_time: Mapped[str] = mapped_column(String(5), default="08:45")
    subject: Mapped[str] = mapped_column(String(512))
    body_html: Mapped[str] = mapped_column(Text)
    registration_subject: Mapped[str] = mapped_column(String(512), default="")
    registration_body_html: Mapped[str] = mapped_column(Text, default="")
    recovery_subject: Mapped[str] = mapped_column(String(512), default="")
    recovery_body_html: Mapped[str] = mapped_column(Text, default="")
    updated_by: Mapped[str] = mapped_column(String(256), default="")
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=utcnow,
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=utcnow,
        onupdate=utcnow,
    )


class TechExpertNotification(Base):
    """Одно письмо в Техэксперт на один кадровый эпизод организации."""

    __tablename__ = "techexpert_notifications"
    __table_args__ = (
        UniqueConstraint(
            "employment_event_id",
            name="uq_techexpert_notification_employment_event",
        ),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    employment_event_id: Mapped[int] = mapped_column(Integer, index=True)
    worker_key: Mapped[str] = mapped_column(String(64), index=True)
    source_id: Mapped[str] = mapped_column(String(128), index=True)
    source_name: Mapped[str] = mapped_column(String(256), default="")
    fio: Mapped[str] = mapped_column(String(512), default="")
    department: Mapped[str] = mapped_column(String(512), default="")
    corporate_email: Mapped[str] = mapped_column(String(320), default="")
    ad_login: Mapped[str] = mapped_column(String(128), default="")
    ad_object_guid: Mapped[str] = mapped_column(String(64), default="")
    dismissal_date: Mapped[date] = mapped_column(Date, index=True)
    deferred_until: Mapped[date | None] = mapped_column(Date, nullable=True)
    hr_reason: Mapped[str] = mapped_column(String(64), default="")
    event_updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    recipient_email: Mapped[str] = mapped_column(String(320), default="")
    scheduled_for: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        index=True,
    )
    status: Mapped[str] = mapped_column(
        String(32),
        default="pending",
        index=True,
    )
    membership_state: Mapped[str] = mapped_column(
        String(32),
        default="not_checked",
    )
    group_removal_status: Mapped[str] = mapped_column(
        String(32),
        default="not_started",
        index=True,
    )
    group_removed_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
    )
    group_removal_error: Mapped[str] = mapped_column(Text, default="")
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    last_error: Mapped[str] = mapped_column(Text, default="")
    next_attempt_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
        index=True,
    )
    sent_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
    )
    cancelled_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
    )
    attention_state: Mapped[str] = mapped_column(
        String(64),
        default="",
        index=True,
    )
    attention_details: Mapped[str] = mapped_column(Text, default="")
    attention_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=utcnow,
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=utcnow,
        onupdate=utcnow,
    )


class TechExpertNotificationBatch(Base):
    """Одна пакетная попытка отправки письма в Техэксперт."""

    __tablename__ = "techexpert_notification_batches"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    source_id: Mapped[str] = mapped_column(String(128), index=True)
    source_name: Mapped[str] = mapped_column(String(256), default="")
    recipient_email: Mapped[str] = mapped_column(String(320), default="")
    subject: Mapped[str] = mapped_column(String(512), default="")
    status: Mapped[str] = mapped_column(
        String(32),
        default="processing",
        index=True,
    )
    total_count: Mapped[int] = mapped_column(Integer, default=0)
    included_count: Mapped[int] = mapped_column(Integer, default=0)
    excluded_count: Mapped[int] = mapped_column(Integer, default=0)
    last_error: Mapped[str] = mapped_column(Text, default="")
    sent_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
    )
    completed_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=utcnow,
        index=True,
    )


class TechExpertNotificationBatchItem(Base):
    """Снимок решения по одному работнику внутри пакетной попытки."""

    __tablename__ = "techexpert_notification_batch_items"
    __table_args__ = (
        UniqueConstraint(
            "batch_id",
            "notification_id",
            name="uq_techexpert_batch_notification",
        ),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    batch_id: Mapped[int] = mapped_column(Integer, index=True)
    notification_id: Mapped[int] = mapped_column(Integer, index=True)
    worker_key: Mapped[str] = mapped_column(String(64), index=True)
    fio: Mapped[str] = mapped_column(String(512), default="")
    department: Mapped[str] = mapped_column(String(512), default="")
    corporate_email: Mapped[str] = mapped_column(String(320), default="")
    ad_login: Mapped[str] = mapped_column(String(128), default="")
    dismissal_date: Mapped[date] = mapped_column(Date)
    membership_state: Mapped[str] = mapped_column(
        String(32),
        default="not_checked",
    )
    included: Mapped[bool] = mapped_column(Boolean, default=False)
    result: Mapped[str] = mapped_column(
        String(32),
        default="pending",
        index=True,
    )
    reason: Mapped[str] = mapped_column(Text, default="")
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=utcnow,
    )


class TechExpertActualizationRun(Base):
    """Последовательно наполняемый пакет первичной сверки Техэксперта."""

    __tablename__ = "techexpert_actualization_runs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    source_id: Mapped[str] = mapped_column(String(128), index=True)
    title: Mapped[str] = mapped_column(String(256), default="")
    status: Mapped[str] = mapped_column(String(32), default="open", index=True)
    files_count: Mapped[int] = mapped_column(Integer, default=0)
    total_count: Mapped[int] = mapped_column(Integer, default=0)
    working_count: Mapped[int] = mapped_column(Integer, default=0)
    not_working_count: Mapped[int] = mapped_column(Integer, default=0)
    review_count: Mapped[int] = mapped_column(Integer, default=0)
    ad_found_count: Mapped[int] = mapped_column(Integer, default=0)
    created_by: Mapped[str] = mapped_column(String(256), default="")
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=utcnow,
        index=True,
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=utcnow,
        onupdate=utcnow,
    )
    completed_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
    )


class TechExpertActualizationFile(Base):
    """Один исходный XLSX внутри пакета актуализации."""

    __tablename__ = "techexpert_actualization_files"
    __table_args__ = (
        UniqueConstraint(
            "run_id",
            "filename",
            name="uq_techexpert_actualization_file",
        ),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    run_id: Mapped[int] = mapped_column(Integer, index=True)
    filename: Mapped[str] = mapped_column(String(512), default="")
    department_name: Mapped[str] = mapped_column(String(512), default="")
    rows_count: Mapped[int] = mapped_column(Integer, default=0)
    working_count: Mapped[int] = mapped_column(Integer, default=0)
    not_working_count: Mapped[int] = mapped_column(Integer, default=0)
    review_count: Mapped[int] = mapped_column(Integer, default=0)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=utcnow,
    )


class TechExpertActualizationItem(Base):
    """Одна исходная строка XLSX внутри операции актуализации."""

    __tablename__ = "techexpert_actualization_items"
    __table_args__ = (
        UniqueConstraint(
            "file_id",
            "source_row",
            name="uq_techexpert_actualization_item_row",
        ),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    run_id: Mapped[int] = mapped_column(Integer, index=True)
    file_id: Mapped[int] = mapped_column(Integer, index=True)
    source_row: Mapped[int] = mapped_column(Integer)
    source_department: Mapped[str] = mapped_column(String(512), default="")
    source_fio: Mapped[str] = mapped_column(String(512), default="")
    normalized_fio: Mapped[str] = mapped_column(String(512), default="", index=True)
    source_position: Mapped[str] = mapped_column(String(512), default="")
    source_email: Mapped[str] = mapped_column(String(320), default="")
    source_phone: Mapped[str] = mapped_column(String(128), default="")
    source_login: Mapped[str] = mapped_column(String(256), default="")
    source_password_encrypted: Mapped[str] = mapped_column(Text, default="")
    category: Mapped[str] = mapped_column(String(32), index=True)
    reason: Mapped[str] = mapped_column(Text, default="")
    candidates_json: Mapped[str] = mapped_column(Text, default="[]")
    worker_key: Mapped[str] = mapped_column(String(64), default="", index=True)
    hr_record_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    current_fio: Mapped[str] = mapped_column(String(512), default="")
    current_positions: Mapped[str] = mapped_column(Text, default="")
    current_departments: Mapped[str] = mapped_column(Text, default="")
    ad_login: Mapped[str] = mapped_column(String(128), default="")
    ad_object_guid: Mapped[str] = mapped_column(String(64), default="")
    ad_distinguished_name: Mapped[str] = mapped_column(String(2048), default="")
    ad_status: Mapped[str] = mapped_column(String(32), default="not_checked")
    membership_state: Mapped[str] = mapped_column(String(32), default="not_checked")
    group_action: Mapped[str] = mapped_column(String(32), default="not_started")
    group_action_error: Mapped[str] = mapped_column(Text, default="")
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=utcnow,
    )


class TechExpertRegistrationRequest(Base):
    """Подготовленный запрос на регистрацию или восстановление доступа."""

    __tablename__ = "techexpert_registration_requests"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    request_kind: Mapped[str] = mapped_column(
        String(32), default="registration", index=True
    )
    worker_key: Mapped[str] = mapped_column(String(64), index=True)
    source_id: Mapped[str] = mapped_column(String(128), index=True)
    source_name: Mapped[str] = mapped_column(String(256), default="")
    hr_record_id: Mapped[int] = mapped_column(Integer, index=True)
    fio: Mapped[str] = mapped_column(String(512), default="")
    department: Mapped[str] = mapped_column(String(512), default="")
    placement_department: Mapped[str] = mapped_column(String(1024), default="")
    position: Mapped[str] = mapped_column(String(512), default="")
    corporate_email: Mapped[str] = mapped_column(String(320), default="")
    mobile_phone: Mapped[str] = mapped_column(String(128), default="")
    ad_login: Mapped[str] = mapped_column(String(128), default="")
    ad_object_guid: Mapped[str] = mapped_column(String(64), default="")
    recipient_email: Mapped[str] = mapped_column(String(320), default="")
    sender_email: Mapped[str] = mapped_column(String(320), default="")
    sender_name: Mapped[str] = mapped_column(String(256), default="")
    subject: Mapped[str] = mapped_column(String(512), default="")
    body_html: Mapped[str] = mapped_column(Text, default="")
    status: Mapped[str] = mapped_column(String(32), default="draft", index=True)
    group_status: Mapped[str] = mapped_column(
        String(32), default="not_started", index=True
    )
    group_error: Mapped[str] = mapped_column(Text, default="")
    email_status: Mapped[str] = mapped_column(
        String(32), default="not_started", index=True
    )
    email_error: Mapped[str] = mapped_column(Text, default="")
    last_error: Mapped[str] = mapped_column(Text, default="")
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    queued_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True, index=True
    )
    scheduled_for: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True, index=True
    )
    next_attempt_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True, index=True
    )
    created_by: Mapped[str] = mapped_column(String(256), default="", index=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, index=True
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, onupdate=utcnow
    )
    sent_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
