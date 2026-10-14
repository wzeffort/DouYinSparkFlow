"""Additive push tables, independent of the existing email queue."""
from datetime import datetime

from sqlalchemy import Boolean, DateTime, ForeignKey, Integer, LargeBinary, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from spark_console.models import Base, utc_now, uuid_string


class PushChannel(Base):
    __tablename__ = "push_channels"
    __table_args__ = (UniqueConstraint("user_id", "channel"),)

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uuid_string)
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    channel: Mapped[str] = mapped_column(String(12))
    credentials: Mapped[bytes] = mapped_column(LargeBinary)
    nonce: Mapped[bytes] = mapped_column(LargeBinary)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    task_success: Mapped[bool] = mapped_column(Boolean, default=False)
    task_failure: Mapped[bool] = mapped_column(Boolean, default=True)
    login_expired: Mapped[bool] = mapped_column(Boolean, default=True)
    enabled_since: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)
    last_test_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_attempt_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    blocked_code: Mapped[str | None] = mapped_column(String(48))


class PushDelivery(Base):
    __tablename__ = "push_deliveries"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uuid_string)
    channel_id: Mapped[str] = mapped_column(ForeignKey("push_channels.id", ondelete="CASCADE"), index=True)
    dedupe_key: Mapped[str] = mapped_column(String(180), unique=True)
    kind: Mapped[str] = mapped_column(String(24))
    source_id: Mapped[str | None] = mapped_column(String(36))
    payload: Mapped[bytes | None] = mapped_column(LargeBinary)
    nonce: Mapped[bytes | None] = mapped_column(LargeBinary)
    status: Mapped[str] = mapped_column(String(16), default="pending", index=True)
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    next_attempt_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now, index=True)
    claimed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)
    provider_id: Mapped[str | None] = mapped_column(String(128))
    error_code: Mapped[str | None] = mapped_column(String(48))
