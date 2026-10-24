from datetime import datetime
from sqlalchemy import DateTime, Integer, String
from sqlalchemy.orm import Mapped, mapped_column
from spark_console.models import Base, utc_now


class QuotaGiftReceipt(Base):
    __tablename__ = 'quota_gift_receipts'
    campaign: Mapped[str] = mapped_column(String(64), primary_key=True)
    user_id: Mapped[str] = mapped_column(String(36), primary_key=True)
    grant_id: Mapped[str] = mapped_column(String(36))
    days: Mapped[int] = mapped_column(Integer)
    expires_before: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    expires_after: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    email_event_id: Mapped[str] = mapped_column(String(36))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)
