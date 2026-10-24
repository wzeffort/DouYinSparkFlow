"""Durable referral records deliberately survive account/invitation deletion."""
from datetime import datetime
from sqlalchemy import Boolean, DateTime, ForeignKey, Integer, String
from sqlalchemy.orm import Mapped, mapped_column
from spark_console.models import Base, utc_now


class ReferralCode(Base):
    __tablename__ = "referral_codes"
    invite_id: Mapped[str] = mapped_column(String(36), primary_key=True)
    inviter_id: Mapped[str] = mapped_column(String(36), index=True)


class Referral(Base):
    __tablename__ = "referrals"
    friend_id: Mapped[str] = mapped_column(String(36), primary_key=True)
    inviter_id: Mapped[str] = mapped_column(String(36), index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)
    rewarded_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    inviter_days: Mapped[int] = mapped_column(Integer, default=0)
    friend_days: Mapped[int] = mapped_column(Integer, default=0)


class VerifiedQQ(Base):
    __tablename__ = "referral_verified_qq"
    email_hash: Mapped[str] = mapped_column(String(64), primary_key=True)
    first_user_id: Mapped[str] = mapped_column(String(36), index=True)
    verified_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)


class ReferralLock(Base):
    __tablename__ = "referral_lock"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    version: Mapped[int] = mapped_column(Integer, default=0)


class ReferralOutcome(Base):
    __tablename__ = "referral_outcomes"
    friend_id: Mapped[str] = mapped_column(String(36), primary_key=True)
    inviter_name: Mapped[str] = mapped_column(String(32))
    friend_name: Mapped[str] = mapped_column(String(32))
    status: Mapped[str] = mapped_column(String(16), default="pending", index=True)
    reason: Mapped[str] = mapped_column(String(48), default="qq_pending")
    checked_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)


class ReferralReceipt(Base):
    __tablename__ = "referral_receipts"
    friend_id: Mapped[str] = mapped_column(String(36), primary_key=True)
    recipient: Mapped[str] = mapped_column(String(16), primary_key=True)
    user_id: Mapped[str] = mapped_column(String(36))
    grant_id: Mapped[str] = mapped_column(String(36))
    days: Mapped[int] = mapped_column(Integer)
    expires_before: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    expires_after: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    unlimited: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)


class VerifiedDouyin(Base):
    __tablename__ = "referral_verified_douyin"
    platform_user_id: Mapped[str] = mapped_column(String(32), primary_key=True)
    first_user_id: Mapped[str] = mapped_column(String(36), index=True)
    verified_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)


class AccountReferralIdentity(Base):
    __tablename__ = "account_referral_identities"
    account_id: Mapped[str] = mapped_column(ForeignKey("douyin_accounts.id", ondelete="CASCADE"), primary_key=True)
    platform_user_id: Mapped[str] = mapped_column(String(32), index=True)
