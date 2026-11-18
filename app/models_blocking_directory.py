"""Read-only account search index, separate from HR and lifecycle decisions."""
from datetime import datetime, timezone

from sqlalchemy import Boolean, DateTime, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from app.db import Base


def utcnow():
    return datetime.now(timezone.utc)


class BlockingDirectoryAccount(Base):
    __tablename__ = 'blocking_directory_accounts'
    __table_args__ = (UniqueConstraint('system', 'account_key'),)
    id: Mapped[int] = mapped_column(primary_key=True)
    system: Mapped[str] = mapped_column(String(16), index=True)
    account_key: Mapped[str] = mapped_column(String(384))
    fio: Mapped[str] = mapped_column(String(512), default='')
    login: Mapped[str] = mapped_column(String(128), default='')
    email: Mapped[str] = mapped_column(String(320), default='')
    ad_login: Mapped[str] = mapped_column(String(128), default='')
    ad_guid: Mapped[str] = mapped_column(String(64), default='')
    zimbra_email: Mapped[str] = mapped_column(String(320), default='')
    zimbra_id: Mapped[str] = mapped_column(String(128), default='')
    status: Mapped[str] = mapped_column(String(32), default='')
    search_text: Mapped[str] = mapped_column(Text, default='')
    present: Mapped[bool] = mapped_column(Boolean, default=True)
    checked_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class BlockingDirectorySync(Base):
    __tablename__ = 'blocking_directory_sync'
    system: Mapped[str] = mapped_column(String(16), primary_key=True)
    last_success_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    next_attempt_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    error: Mapped[str] = mapped_column(Text, default='')
