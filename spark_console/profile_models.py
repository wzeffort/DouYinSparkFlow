from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, Integer, LargeBinary, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from spark_console.models import Base


class UserProfile(Base):
    __tablename__ = 'user_profiles'

    user_id: Mapped[str] = mapped_column(ForeignKey('users.id', ondelete='CASCADE'), primary_key=True)
    nickname: Mapped[str | None] = mapped_column(String(32))
    age: Mapped[int | None] = mapped_column(Integer)
    gender: Mapped[str] = mapped_column(String(16), default='private', nullable=False)
    bio: Mapped[str | None] = mapped_column(String(160))
    avatar_image: Mapped[bytes | None] = mapped_column(LargeBinary)
    avatar_version: Mapped[str | None] = mapped_column(String(36))
    username_changed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    security_failed_attempts: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    security_locked_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class EmailBindingProof(Base):
    __tablename__ = 'email_binding_proofs'

    request_id: Mapped[str] = mapped_column(
        ForeignKey('email_verification_requests.id', ondelete='CASCADE'), primary_key=True)
    source_email_lookup_hash: Mapped[str | None] = mapped_column(String(64))
    source_password_tag: Mapped[str] = mapped_column(String(64), nullable=False)
    old_code_hash: Mapped[str | None] = mapped_column(String(64))
