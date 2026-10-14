from datetime import datetime
from sqlalchemy import DateTime, ForeignKey, Integer, LargeBinary, String
from sqlalchemy.orm import Mapped, mapped_column
from spark_console.models import Base, utc_now, uuid_string


class RenewalOrder(Base):
    __tablename__ = 'renewal_orders'
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uuid_string)
    user_id: Mapped[str] = mapped_column(ForeignKey('users.id'), index=True)
    plan_key: Mapped[str] = mapped_column(String(24))
    price_cents: Mapped[int] = mapped_column(Integer)
    days: Mapped[int] = mapped_column(Integer)
    channel: Mapped[str] = mapped_column(String(12))
    status: Mapped[str] = mapped_column(String(16), default='pending', index=True)
    target_grant_id: Mapped[str | None] = mapped_column(String(36))
    fulfilled_grant_id: Mapped[str | None] = mapped_column(String(36))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)
    submitted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    reviewed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    reviewer_id: Mapped[str | None] = mapped_column(String(36))


class RenewalPaymentCode(Base):
    __tablename__ = 'renewal_payment_codes'
    channel: Mapped[str] = mapped_column(String(12), primary_key=True)
    image: Mapped[bytes] = mapped_column(LargeBinary)
    content_type: Mapped[str] = mapped_column(String(24))
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)


class RenewalPromotionClaim(Base):
    __tablename__ = 'renewal_promotion_claims'
    user_id: Mapped[str] = mapped_column(ForeignKey('users.id'), primary_key=True)
    promotion: Mapped[str] = mapped_column(String(24), primary_key=True)
    order_id: Mapped[str] = mapped_column(ForeignKey('renewal_orders.id'), index=True)
    used_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
