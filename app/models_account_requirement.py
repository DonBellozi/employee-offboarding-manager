"""Operator labels, not a second employee or account registry."""
from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import Boolean, CheckConstraint, DateTime, Float, ForeignKey, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.db import Base


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


class AccountRequirementCase(Base):
    __tablename__ = "account_requirement_cases"
    __table_args__ = (
        CheckConstraint("state IN ('pending','clarification','decided','cancelled')"),
        CheckConstraint("decision IS NULL OR decision IN ('required','not_required')"),
        CheckConstraint("state != 'decided' OR decision IS NOT NULL"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    worker_key: Mapped[str] = mapped_column(String(64), index=True)
    employment_arrival_id: Mapped[int] = mapped_column(
        ForeignKey("hr_employment_arrival_events.id"), unique=True,
    )
    fio: Mapped[str] = mapped_column(String(512), default="")
    state: Mapped[str] = mapped_column(String(32), default="pending", index=True)
    decision: Mapped[str | None] = mapped_column(String(32), nullable=True, index=True)
    clarification_required: Mapped[bool] = mapped_column(Boolean, default=False)
    decision_source: Mapped[str] = mapped_column(String(32), default="")
    initial_snapshot_json: Mapped[str] = mapped_column(Text)
    decision_snapshot_json: Mapped[str | None] = mapped_column(Text, nullable=True)
    snapshot_schema_version: Mapped[int] = mapped_column(Integer, default=1)
    comment: Mapped[str] = mapped_column(Text, default="")
    decided_by: Mapped[str] = mapped_column(String(256), default="")
    decided_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_account: Mapped[bool] = mapped_column(Boolean, default=False)
    external_action_status: Mapped[str] = mapped_column(String(32), default="not_started")
    provisioning_operation_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    model_version: Mapped[str | None] = mapped_column(String(128), nullable=True)
    model_score: Mapped[float | None] = mapped_column(Float, nullable=True)
    model_recommendation: Mapped[str | None] = mapped_column(String(32), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow)
    revision: Mapped[int] = mapped_column(Integer, default=1, nullable=False)
    __mapper_args__ = {"version_id_col": revision}


class AccountRequirementArrival(Base):
    """One person's simultaneous arrivals share a case; no duplicate labels."""
    __tablename__ = "account_requirement_arrivals"
    arrival_id: Mapped[int] = mapped_column(ForeignKey("hr_employment_arrival_events.id"), primary_key=True)
    case_id: Mapped[int] = mapped_column(ForeignKey("account_requirement_cases.id"), index=True)


class AccountRequirementDecisionEvent(Base):
    __tablename__ = "account_requirement_decision_events"
    id: Mapped[int] = mapped_column(primary_key=True)
    case_id: Mapped[int] = mapped_column(ForeignKey("account_requirement_cases.id"), index=True)
    previous_decision: Mapped[str | None] = mapped_column(String(32), nullable=True)
    new_decision: Mapped[str | None] = mapped_column(String(32), nullable=True)
    previous_state: Mapped[str] = mapped_column(String(32), default="")
    new_state: Mapped[str] = mapped_column(String(32))
    actor: Mapped[str] = mapped_column(String(256))
    comment: Mapped[str] = mapped_column(Text, default="")
    snapshot_json: Mapped[str] = mapped_column(Text)
    snapshot_hash: Mapped[str] = mapped_column(String(64))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
