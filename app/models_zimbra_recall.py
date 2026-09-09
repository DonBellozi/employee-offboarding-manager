from __future__ import annotations

from datetime import date, datetime, timezone

from sqlalchemy import Date, DateTime, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.db import Base


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


class ZimbraMailRecallRun(Base):
    """Разовый отзыв ошибочно отправленного сообщения из ящиков Zimbra."""

    __tablename__ = "zimbra_mail_recall_runs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    status: Mapped[str] = mapped_column(
        String(32), default="queued", index=True
    )
    initiated_by: Mapped[str] = mapped_column(String(256), default="")
    batch_id: Mapped[int] = mapped_column(Integer, default=0, index=True)
    lookup_mailbox: Mapped[str] = mapped_column(String(320), default="")

    sender_email: Mapped[str] = mapped_column(String(320), index=True)
    author_mailbox: Mapped[str] = mapped_column(String(320), default="")
    recipient_email: Mapped[str] = mapped_column(String(320), default="")
    subject_hint: Mapped[str] = mapped_column(String(512), default="")
    sent_date: Mapped[date] = mapped_column(Date)
    approximate_time: Mapped[str] = mapped_column(String(5))
    time_window_minutes: Mapped[int] = mapped_column(Integer, default=30)

    message_id: Mapped[str] = mapped_column(String(998), default="", index=True)
    source_local_id: Mapped[str] = mapped_column(String(64), default="")
    source_subject: Mapped[str] = mapped_column(String(998), default="")
    source_sent_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    source_search_query: Mapped[str] = mapped_column(Text, default="")
    candidates_json: Mapped[str] = mapped_column(Text, default="[]")

    total_mailboxes: Mapped[int] = mapped_column(Integer, default=0)
    processed_mailboxes: Mapped[int] = mapped_column(Integer, default=0)
    matched_mailboxes: Mapped[int] = mapped_column(Integer, default=0)
    found_messages: Mapped[int] = mapped_column(Integer, default=0)
    deleted_messages: Mapped[int] = mapped_column(Integer, default=0)
    remaining_messages: Mapped[int] = mapped_column(Integer, default=0)
    error_count: Mapped[int] = mapped_column(Integer, default=0)
    duration_ms: Mapped[int] = mapped_column(Integer, default=0)
    details_json: Mapped[str] = mapped_column(Text, default="[]")
    error_message: Mapped[str] = mapped_column(Text, default="")

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, index=True
    )
    started_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    progress_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    completed_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )


class ZimbraMailRecallBatch(Base):
    """Фиксированный набор запросов для одного прохода по ящикам."""

    __tablename__ = "zimbra_mail_recall_batches"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    status: Mapped[str] = mapped_column(String(32), default="queued", index=True)
    initiated_by: Mapped[str] = mapped_column(String(256), default="")
    total_mailboxes: Mapped[int] = mapped_column(Integer, default=0)
    processed_mailboxes: Mapped[int] = mapped_column(Integer, default=0)
    found_messages: Mapped[int] = mapped_column(Integer, default=0)
    deleted_messages: Mapped[int] = mapped_column(Integer, default=0)
    remaining_messages: Mapped[int] = mapped_column(Integer, default=0)
    error_count: Mapped[int] = mapped_column(Integer, default=0)
    duration_ms: Mapped[int] = mapped_column(Integer, default=0)
    error_message: Mapped[str] = mapped_column(Text, default="")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
