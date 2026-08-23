from __future__ import annotations

from datetime import date, datetime, timezone

from sqlalchemy import Date, DateTime, Integer, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from app.db import Base


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


class FinalDismissalAutomationState(Base):
    """Момент включения боевой автоматической блокировки."""

    __tablename__ = "final_dismissal_automation_state"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    activated_on: Mapped[date] = mapped_column(Date, index=True)
    activated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=utcnow,
    )


class FinalDismissalBlockRun(Base):
    """Один эпизод автоматической блокировки окончательно уволенного человека."""

    __tablename__ = "final_dismissal_block_runs"
    __table_args__ = (
        UniqueConstraint(
            "worker_key",
            "dismissal_date",
            name="uq_final_dismissal_block_worker_date",
        ),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    worker_key: Mapped[str] = mapped_column(String(64), index=True)
    dismissal_date: Mapped[date] = mapped_column(Date, index=True)
    effective_block_date: Mapped[date] = mapped_column(Date, index=True)
    fio: Mapped[str] = mapped_column(String(512), default="")
    status: Mapped[str] = mapped_column(
        String(32),
        default="pending",
        index=True,
    )
    last_error: Mapped[str] = mapped_column(Text, default="")
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=utcnow,
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
    cancelled_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
    )


class FinalDismissalBlockTarget(Base):
    """Один внешний объект, который требуется заблокировать."""

    __tablename__ = "final_dismissal_block_targets"
    __table_args__ = (
        UniqueConstraint(
            "run_id",
            "system",
            "target_key",
            name="uq_final_dismissal_block_target",
        ),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    run_id: Mapped[int] = mapped_column(Integer, index=True)
    system: Mapped[str] = mapped_column(String(32), index=True)
    target_key: Mapped[str] = mapped_column(String(512))
    target_identifier: Mapped[str] = mapped_column(String(320), default="")
    stable_id: Mapped[str] = mapped_column(String(128), default="")
    status: Mapped[str] = mapped_column(
        String(32),
        default="pending",
        index=True,
    )
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    last_error: Mapped[str] = mapped_column(Text, default="")
    last_result: Mapped[str] = mapped_column(String(64), default="")
    next_attempt_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
        index=True,
    )
    last_attempt_at: Mapped[datetime | None] = mapped_column(
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
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=utcnow,
        onupdate=utcnow,
    )


class ADReactivationAlert(Base):
    """Операторское состояние: заблокированный в AD снова активен в HR."""

    __tablename__ = "ad_reactivation_alerts"
    __table_args__ = (
        UniqueConstraint("worker_key", name="uq_ad_reactivation_alert_worker"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    worker_key: Mapped[str] = mapped_column(String(64), index=True)
    block_run_id: Mapped[int] = mapped_column(Integer, default=0, index=True)
    dismissal_date: Mapped[date | None] = mapped_column(Date, nullable=True)
    fio: Mapped[str] = mapped_column(String(512), default="")
    ad_login: Mapped[str] = mapped_column(String(320), default="")
    ad_object_guid: Mapped[str] = mapped_column(String(128), default="")
    status: Mapped[str] = mapped_column(String(32), default="open", index=True)
    resolution: Mapped[str] = mapped_column(String(32), default="")
    resolved_by: Mapped[str] = mapped_column(String(256), default="")
    resolved_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    last_error: Mapped[str] = mapped_column(Text, default="")
    candidates_json: Mapped[str] = mapped_column(Text, default="[]")
    last_checked_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    details: Mapped[str] = mapped_column(Text, default="")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class DismissalDetailsSnapshot(Base):
    """Последний фоновый read-only снимок систем для одного увольнения."""

    __tablename__ = "dismissal_details_snapshots"
    __table_args__ = (
        UniqueConstraint(
            "worker_key",
            "dismissal_date",
            name="uq_dismissal_details_snapshot_worker_date",
        ),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    worker_key: Mapped[str] = mapped_column(String(64), index=True)
    dismissal_date: Mapped[date] = mapped_column(Date, index=True)
    candidate_fingerprint: Mapped[str] = mapped_column(String(64), default="")
    payload_json: Mapped[str] = mapped_column(Text, default="{}")
    status: Mapped[str] = mapped_column(String(32), default="pending", index=True)
    last_error: Mapped[str] = mapped_column(Text, default="")
    checked_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
    )
    last_attempt_at: Mapped[datetime | None] = mapped_column(
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
