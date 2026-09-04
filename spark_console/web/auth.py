from __future__ import annotations

import secrets
from datetime import datetime, timezone

from fastapi import HTTPException, Request
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from spark_console.models import User, UserNotification, WebSession
from spark_console.security import SessionService


def _aware(value: datetime) -> datetime:
    return value if value.tzinfo else value.replace(tzinfo=timezone.utc)


class WebAuth:
    def __init__(self, sessions: SessionService):
        self.sessions = sessions

    def current(
        self, request: Request, db: Session, allow_change: bool = False
    ) -> tuple[User, WebSession]:
        raw = request.cookies.get("spark_session")
        if not raw:
            raise HTTPException(401)
        record = db.scalar(
            select(WebSession).where(WebSession.token_hash == self.sessions.token_hash(raw))
        )
        if record is None or _aware(record.expires_at) <= datetime.now(timezone.utc):
            raise HTTPException(401)
        user = db.get(User, record.user_id)
        if user is None or user.status != "active":
            raise HTTPException(401)
        if user.must_change_password and not allow_change:
            raise HTTPException(409, "password-change-required")
        return user, record

    def csrf(self, record: WebSession, supplied: str) -> None:
        if not supplied or not secrets.compare_digest(record.csrf_token, supplied):
            raise HTTPException(403, "CSRF validation failed")

    @staticmethod
    def _page_context(db: Session, user: User, record: WebSession) -> dict:
        unread = db.scalar(
            select(func.count(UserNotification.id)).where(
                UserNotification.user_id == user.id,
                UserNotification.read_at.is_(None),
            )
        ) or 0
        return {
            "user": user,
            "csrf_token": record.csrf_token,
            "is_admin": user.role == "admin",
            "unread_notification_count": unread,
        }

    def user_context(
        self, request: Request, db: Session
    ) -> tuple[User, WebSession, dict]:
        user, record = self.current(request, db)
        return user, record, self._page_context(db, user, record)

    def admin_context(
        self, request: Request, db: Session
    ) -> tuple[User, WebSession, dict]:
        user, record = self.current(request, db)
        if user.role != "admin":
            raise HTTPException(404)
        return user, record, self._page_context(db, user, record)
