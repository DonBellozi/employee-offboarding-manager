from __future__ import annotations

from datetime import date, datetime, timezone
from enum import StrEnum
from sqlalchemy import Boolean, Date, DateTime, Enum, Integer, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column
from app.db import Base

def utcnow() -> datetime:
    return datetime.now(timezone.utc)

class UserRole(StrEnum):
    ADMIN = "admin"
    OPERATOR = "operator"
    VIEWER = "viewer"

class OperationStatus(StrEnum):
    DRAFT = "draft"
    RUNNING = "running"
    PARTIAL = "partial"
    SUCCESS = "success"
    FAILED = "failed"

class LocalUser(Base):
    __tablename__ = "local_users"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    username: Mapped[str] = mapped_column(String(128), unique=True, index=True)
    password_hash: Mapped[str] = mapped_column(String(512))
    role: Mapped[UserRole] = mapped_column(Enum(UserRole), default=UserRole.ADMIN)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

class DomainAccessUser(Base):
    __tablename__ = "domain_access_users"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    username: Mapped[str] = mapped_column(String(128), unique=True, index=True)
    display_name: Mapped[str] = mapped_column(String(256), default="")
    email: Mapped[str] = mapped_column(String(320), default="")
    role: Mapped[UserRole] = mapped_column(Enum(UserRole), default=UserRole.OPERATOR)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    created_by: Mapped[str] = mapped_column(String(256), default="")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow)

class DomainMailProfile(Base):
    __tablename__ = "domain_mail_profiles"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    domain: Mapped[str] = mapped_column(String(255), unique=True, index=True)
    sender_name: Mapped[str] = mapped_column(String(256), default="")
    sender_email: Mapped[str] = mapped_column(String(320))
    personal_subject: Mapped[str] = mapped_column(String(512))
    personal_body_html: Mapped[str] = mapped_column(Text)
    corporate_subject: Mapped[str] = mapped_column(String(512))
    corporate_body_html: Mapped[str] = mapped_column(Text)
    updated_by: Mapped[str] = mapped_column(String(256), default="")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow)

class ProvisioningOperation(Base):
    __tablename__ = "provisioning_operations"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    operation_kind: Mapped[str] = mapped_column(String(32), default="full")
    operator_username: Mapped[str] = mapped_column(String(256), index=True)
    last_name: Mapped[str] = mapped_column(String(128))
    first_name: Mapped[str] = mapped_column(String(128))
    middle_name: Mapped[str] = mapped_column(String(128), default="")
    personal_email: Mapped[str] = mapped_column(String(320))
    login: Mapped[str] = mapped_column(String(64), index=True)
    corporate_email: Mapped[str] = mapped_column(String(320), index=True)
    mail_domain: Mapped[str] = mapped_column(String(255))
    status: Mapped[OperationStatus] = mapped_column(Enum(OperationStatus), default=OperationStatus.DRAFT)
    ad_created: Mapped[bool] = mapped_column(Boolean, default=False)
    ad_enabled: Mapped[bool] = mapped_column(Boolean, default=False)
    zimbra_created: Mapped[bool] = mapped_column(Boolean, default=False)
    personal_mail_sent: Mapped[bool] = mapped_column(Boolean, default=False)
    corporate_mail_sent: Mapped[bool] = mapped_column(Boolean, default=False)
    error_message: Mapped[str] = mapped_column(Text, default="")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

class ADProvisioningOperation(Base):
    """Создание только AD для работника с уже существующей корпоративной почтой."""

    __tablename__ = "ad_provisioning_operations"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    worker_key: Mapped[str] = mapped_column(String(64), index=True)
    source_id: Mapped[str] = mapped_column(String(128), index=True)
    operator_username: Mapped[str] = mapped_column(String(256), index=True)
    full_name: Mapped[str] = mapped_column(String(512))
    login: Mapped[str] = mapped_column(String(64), index=True)
    corporate_email: Mapped[str] = mapped_column(String(320), index=True)
    status: Mapped[OperationStatus] = mapped_column(
        Enum(OperationStatus),
        default=OperationStatus.DRAFT,
    )
    ad_created: Mapped[bool] = mapped_column(Boolean, default=False)
    ad_enabled: Mapped[bool] = mapped_column(Boolean, default=False)
    credentials_mail_sent: Mapped[bool] = mapped_column(Boolean, default=False)
    registry_updated: Mapped[bool] = mapped_column(Boolean, default=False)
    error_message: Mapped[str] = mapped_column(Text, default="")
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=utcnow,
    )
    completed_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
    )


class BlockingOperation(Base):
    """Ручная блокировка учетных записей из раздела «Блокировка»."""

    __tablename__ = "blocking_operations"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    worker_key: Mapped[str] = mapped_column(String(64), index=True)
    source_id: Mapped[str] = mapped_column(String(128), index=True)
    source_record_id: Mapped[int] = mapped_column(Integer, index=True)
    operator_username: Mapped[str] = mapped_column(String(256), index=True)
    full_name: Mapped[str] = mapped_column(String(512))
    login: Mapped[str] = mapped_column(String(128), index=True)
    corporate_email: Mapped[str] = mapped_column(String(320), default="", index=True)
    status: Mapped[OperationStatus] = mapped_column(
        Enum(OperationStatus),
        default=OperationStatus.RUNNING,
    )
    dry_run: Mapped[bool] = mapped_column(Boolean, default=False)
    ad_disabled: Mapped[bool] = mapped_column(Boolean, default=False)
    zimbra_locked: Mapped[bool] = mapped_column(Boolean, default=False)
    itinvent_checked: Mapped[bool] = mapped_column(Boolean, default=False)
    itinvent_owner_name: Mapped[str] = mapped_column(String(512), default="")
    equipment_count: Mapped[int] = mapped_column(Integer, default=0)
    equipment_snapshot_json: Mapped[str] = mapped_column(Text, default="[]")
    error_message: Mapped[str] = mapped_column(Text, default="")
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=utcnow,
    )
    completed_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
    )


class BlockingQueueItem(Base):
    """Отдельное желаемое состояние AD/Zimbra для операции блокировки."""

    __tablename__ = "blocking_queue_items"
    __table_args__ = (
        UniqueConstraint("operation_id", "system", name="uq_blocking_queue_operation_system"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    operation_id: Mapped[int] = mapped_column(Integer, index=True)
    system: Mapped[str] = mapped_column(String(32), index=True)
    target_identifier: Mapped[str] = mapped_column(String(320), default="")
    stable_id: Mapped[str] = mapped_column(String(128), default="")
    desired_state: Mapped[str] = mapped_column(String(32))
    status: Mapped[str] = mapped_column(String(32), default="pending", index=True)
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    last_error: Mapped[str] = mapped_column(Text, default="")
    last_result: Mapped[str] = mapped_column(String(64), default="")
    last_attempt_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    next_attempt_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True, index=True
    )
    completed_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, onupdate=utcnow
    )


class ITInventControlSettings(Base):
    """Глобальные правила отбора контролируемого имущества IT Invent."""

    __tablename__ = "itinvent_control_settings"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    locations_json: Mapped[str] = mapped_column(Text, default="[]")
    equipment_types_json: Mapped[str] = mapped_column(Text, default="[]")
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


class DismissalSchedule(Base):
    __tablename__ = "dismissal_schedules"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    login: Mapped[str] = mapped_column(String(64), index=True)
    corporate_email: Mapped[str] = mapped_column(String(320), index=True)
    dismissal_date: Mapped[date] = mapped_column(Date)
    operator_username: Mapped[str] = mapped_column(String(256))
    ad_expiration_set: Mapped[bool] = mapped_column(Boolean, default=False)
    zimbra_note_set: Mapped[bool] = mapped_column(Boolean, default=False)
    error_message: Mapped[str] = mapped_column(Text, default="")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

class AuditLog(Base):
    __tablename__ = "audit_log"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    actor: Mapped[str] = mapped_column(String(256), index=True)
    action: Mapped[str] = mapped_column(String(128), index=True)
    target: Mapped[str] = mapped_column(String(320), default="")
    result: Mapped[str] = mapped_column(String(64), default="success")
    details: Mapped[str] = mapped_column(Text, default="")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

class HRPerson(Base):
    """Глобальная карточка человека. СНИЛС в БД не хранится: только HMAC worker_key."""
    __tablename__ = "hr_people"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    worker_key: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    fio: Mapped[str] = mapped_column(String(512), index=True)
    first_seen_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    last_seen_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

class HRSourceRecord(Base):
    """Состояние человека в конкретном кадровом источнике/организации."""
    __tablename__ = "hr_source_records"
    __table_args__ = (UniqueConstraint("worker_key", "source_id", name="uq_hr_source_worker"),)
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    worker_key: Mapped[str] = mapped_column(String(64), index=True)
    source_id: Mapped[str] = mapped_column(String(128), index=True)
    source_name: Mapped[str] = mapped_column(String(256), default="")
    fio: Mapped[str] = mapped_column(String(512), index=True)
    corporate_email: Mapped[str] = mapped_column(String(320), default="", index=True)
    personal_email: Mapped[str] = mapped_column(String(320), default="", index=True)
    mobile_phone: Mapped[str] = mapped_column(String(128), default="")
    login: Mapped[str] = mapped_column(String(128), default="", index=True)
    placements_json: Mapped[str] = mapped_column(Text, default="[]")
    is_present: Mapped[bool] = mapped_column(Boolean, default=True, index=True)
    first_seen_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    last_seen_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    ad_status: Mapped[str] = mapped_column(String(32), default="not_checked", index=True)
    zimbra_status: Mapped[str] = mapped_column(String(32), default="not_checked", index=True)
    # Доступ к Техэксперту относится к конкретной организации, поэтому
    # отметка живет в HRSourceRecord, а не в глобальной карточке HRPerson.
    techexpert_access: Mapped[bool] = mapped_column(
        Boolean,
        default=False,
        index=True,
    )
    reconciliation_status: Mapped[str] = mapped_column(String(32), default="not_checked", index=True)
    reconciliation_error: Mapped[str] = mapped_column(Text, default="")
    reconciled_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

class OneCImportRun(Base):
    """История получения и обработки кадровой выгрузки 1С."""

    __tablename__ = "onec_import_runs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    trigger: Mapped[str] = mapped_column(String(32), default="manual", index=True)
    status: Mapped[str] = mapped_column(String(32), default="running", index=True)
    source_id: Mapped[str] = mapped_column(String(128), default="", index=True)

    mail_uid: Mapped[str] = mapped_column(String(128), default="")
    message_date: Mapped[str] = mapped_column(String(256), default="")
    sender: Mapped[str] = mapped_column(String(512), default="")
    subject: Mapped[str] = mapped_column(String(1024), default="")
    filename: Mapped[str] = mapped_column(String(512), default="")
    file_hash: Mapped[str] = mapped_column(String(64), default="", index=True)
    archive_filename: Mapped[str] = mapped_column(String(768), default="")

    workers_count: Mapped[int] = mapped_column(Integer, default=0)
    placements_count: Mapped[int] = mapped_column(Integer, default=0)
    new_workers: Mapped[int] = mapped_column(Integer, default=0)
    missing_workers: Mapped[int] = mapped_column(Integer, default=0)
    changed_workers: Mapped[int] = mapped_column(Integer, default=0)

    registry_ok: Mapped[int] = mapped_column(Integer, default=0)
    registry_issues: Mapped[int] = mapped_column(Integer, default=0)
    registry_errors: Mapped[int] = mapped_column(Integer, default=0)

    message: Mapped[str] = mapped_column(Text, default="")
    error_message: Mapped[str] = mapped_column(Text, default="")
    started_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=utcnow,
        index=True,
    )
    completed_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
    )


class EmailLoginMapping(Base):
    """Явное сопоставление кадрового работника, AD и Zimbra.

    worker_key is HMAC from SNILS. Plain SNILS is never stored.
    objectGUID and zimbraId keep the link stable if names are changed.
    """

    __tablename__ = "email_login_mappings"
    __table_args__ = (
        UniqueConstraint(
            "worker_key",
            "source_domain",
            name="uq_email_login_mapping_worker_domain",
        ),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    worker_key: Mapped[str] = mapped_column(String(64), index=True)
    source_domain: Mapped[str] = mapped_column(String(255), index=True)
    source_email: Mapped[str] = mapped_column(String(320), index=True)

    ad_object_guid: Mapped[str] = mapped_column(String(64), index=True)
    ad_login: Mapped[str] = mapped_column(String(128), index=True)

    zimbra_id: Mapped[str] = mapped_column(String(128), index=True)
    zimbra_email: Mapped[str] = mapped_column(String(320), index=True)

    created_by: Mapped[str] = mapped_column(String(256), default="")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=utcnow,
        onupdate=utcnow,
    )
    last_verified_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
    )

