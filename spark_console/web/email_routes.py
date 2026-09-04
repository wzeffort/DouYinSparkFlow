from __future__ import annotations

import secrets

from fastapi import APIRouter, Form, HTTPException, Request
from fastapi.responses import RedirectResponse
from sqlalchemy import func, select

from spark_console.db import session_scope
from spark_console.models import (
    AppSetting,
    EmailVerificationRequest,
    NotificationEvent,
    NotificationPreference,
    User,
    UserNotification,
)
from spark_console.pii import PiiCipher, mask_email
from spark_console.rate_limit import FailedAttemptLimiter
from spark_console.security import PasswordService
from spark_console.services import Conflict, ValidationError
from spark_console.services.audits import AuditService
from spark_console.services.email_verification import EmailVerificationService
from spark_console.services.notifications import NotificationService
from spark_console.web.auth import WebAuth
from spark_console.web.registration_routes import registration_client_key


def build_email_router(engine, auth: WebAuth, passwords: PasswordService, pii: PiiCipher, page):
    router = APIRouter()
    reset_limiter = FailedAttemptLimiter(limit=10)

    @router.get("/forgot-password")
    def forgot_password_page(request: Request, verification: str = ""):
        return page(
            request,
            "forgot_password.html",
            title="找回密码",
            nav=False,
            verification_id=verification[:64] or None,
        )

    @router.post("/forgot-password")
    def start_password_reset(request: Request, email: str = Form(default="")):
        client_key = registration_client_key(request)
        request_id = secrets.token_hex(16)
        if reset_limiter.allow(client_key):
            reset_limiter.record_failure(client_key)
            with session_scope(engine) as db:
                verification = EmailVerificationService(
                    db, passwords, pii, AuditService(db)
                ).start_password_reset(email, client_key)
                if verification is not None:
                    request_id = verification.id
        return RedirectResponse(f"/forgot-password?verification={request_id}", 303)

    @router.get("/forgot-password/verify/{request_id}")
    def password_reset_page(request: Request, request_id: str):
        return RedirectResponse(f"/forgot-password?verification={request_id}", 303)

    @router.post("/forgot-password/verify/{request_id}")
    def complete_password_reset(
        request: Request,
        request_id: str,
        code: str = Form(default=""),
        new_password: str = Form(default=""),
        password_confirmation: str = Form(default=""),
    ):
        error = None
        with session_scope(engine) as db:
            try:
                if new_password != password_confirmation:
                    raise ValidationError("两次密码不一致")
                EmailVerificationService(
                    db, passwords, pii, AuditService(db)
                ).complete_password_reset(request_id, code, new_password)
            except (ValidationError, ValueError):
                error = "验证码无效、已过期，或新密码不符合要求"
        if error:
            return page(
                request, "forgot_password.html", 400,
                title="找回密码", nav=False,
                verification_id=request_id, error=error,
            )
        return RedirectResponse("/login?password_reset=1", 303)

    @router.get("/settings/email")
    def email_settings(request: Request):
        with session_scope(engine) as db:
            user, _record, context = auth.user_context(request, db)
            service = EmailVerificationService(db, passwords, pii, AuditService(db))
            email = service.email_for_user(user)
            preference = db.get(NotificationPreference, user.id)
            if preference is None:
                preference = NotificationPreference(user_id=user.id)
                db.add(preference)
                db.flush()
            verification_id = request.query_params.get("verification", "")[:64]
            verification = db.get(EmailVerificationRequest, verification_id)
            if (
                verification is None
                or verification.user_id != user.id
                or verification.purpose != "bind"
                or verification.consumed_at is not None
            ):
                verification_id = None
            return page(
                request, "email_settings.html", title="邮箱与通知",
                email_masked=mask_email(email) if email else None,
                preference=preference,
                verification_id=verification_id,
                notice=request.query_params.get("notice"),
                **context,
            )

    @router.post("/settings/email/start")
    def start_email_binding(
        request: Request,
        email: str = Form(default=""),
        csrf_token: str = Form(default=""),
    ):
        with session_scope(engine) as db:
            user, record, _context = auth.user_context(request, db)
            auth.csrf(record, csrf_token)
            try:
                verification = EmailVerificationService(
                    db, passwords, pii, AuditService(db)
                ).start_binding(user.id, email)
            except (ValidationError, Conflict, ValueError) as exc:
                return RedirectResponse(
                    "/settings/email?notice=" + ("email-in-use" if isinstance(exc, Conflict) else "invalid-email"), 303
                )
        return RedirectResponse(f"/settings/email?verification={verification.id}", 303)

    @router.get("/settings/email/verify/{request_id}")
    def binding_verify_page(request: Request, request_id: str):
        return RedirectResponse(f"/settings/email?verification={request_id}", 303)

    @router.post("/settings/email/verify/{request_id}")
    def verify_binding(
        request: Request,
        request_id: str,
        code: str = Form(default=""),
        csrf_token: str = Form(default=""),
    ):
        with session_scope(engine) as db:
            user, record, context = auth.user_context(request, db)
            auth.csrf(record, csrf_token)
            try:
                EmailVerificationService(
                    db, passwords, pii, AuditService(db)
                ).verify_binding(user.id, request_id, code)
            except (ValidationError, Conflict, ValueError):
                service = EmailVerificationService(db, passwords, pii, AuditService(db))
                email = service.email_for_user(user)
                preference = db.get(NotificationPreference, user.id)
                if preference is None:
                    preference = NotificationPreference(user_id=user.id)
                    db.add(preference)
                    db.flush()
                return page(
                    request, "email_settings.html", 400, title="邮箱与通知",
                    email_masked=mask_email(email) if email else None,
                    preference=preference,
                    verification_id=request_id,
                    notice=None,
                    verification_error="验证码无效或已过期",
                    **context,
                )
        return RedirectResponse("/settings/email?notice=verified", 303)

    @router.post("/settings/email/preferences")
    def update_preferences(
        request: Request,
        csrf_token: str = Form(default=""),
        douyin_login_expired_email: str = Form(default=""),
        task_repeated_failure_email: str = Form(default=""),
        quota_expiring_email: str = Form(default=""),
        quota_expired_email: str = Form(default=""),
    ):
        with session_scope(engine) as db:
            user, record, _context = auth.user_context(request, db)
            auth.csrf(record, csrf_token)
            preference = db.get(NotificationPreference, user.id)
            if preference is None:
                preference = NotificationPreference(user_id=user.id)
                db.add(preference)
            preference.douyin_login_expired_email = bool(douyin_login_expired_email)
            preference.task_repeated_failure_email = bool(task_repeated_failure_email)
            preference.quota_expiring_email = bool(quota_expiring_email)
            preference.quota_expired_email = bool(quota_expired_email)
        return RedirectResponse("/settings/email?notice=saved", 303)

    @router.get("/notifications")
    def notifications_page(request: Request, page_number: int = 1):
        with session_scope(engine) as db:
            user, _record, context = auth.user_context(request, db)
            current = max(1, page_number)
            total = db.scalar(
                select(func.count(UserNotification.id)).where(UserNotification.user_id == user.id)
            ) or 0
            pages = max(1, (total + 5) // 6)
            current = min(current, pages)
            notices = db.scalars(
                select(UserNotification)
                .where(UserNotification.user_id == user.id)
                .order_by(UserNotification.created_at.desc())
                .offset((current - 1) * 6).limit(6)
            ).all()
            return page(
                request, "notifications.html", title="通知中心", notices=notices,
                page_number=current, pages=pages, **context,
            )

    @router.post("/notifications/{notification_id}/read")
    def mark_read(
        request: Request, notification_id: str, csrf_token: str = Form(default="")
    ):
        from datetime import datetime, timezone
        with session_scope(engine) as db:
            user, record, _context = auth.user_context(request, db)
            auth.csrf(record, csrf_token)
            notice = db.get(UserNotification, notification_id)
            if notice is None or notice.user_id != user.id:
                raise HTTPException(404)
            notice.read_at = datetime.now(timezone.utc)
        return RedirectResponse("/notifications#notification-list", 303)

    @router.get("/email-actions/{token}")
    def email_action(request: Request, token: str):
        with session_scope(engine) as db:
            user, _record, _context = auth.user_context(request, db)
            try:
                NotificationService(db, pii, AuditService(db)).consume_action_token(
                    user.id, token
                )
            except ValueError:
                raise HTTPException(404) from None
        return RedirectResponse("/accounts", 303)

    @router.get("/admin/notifications")
    def admin_notifications(request: Request, page_number: int = 1, status: str = "all"):
        with session_scope(engine) as db:
            _admin, _record, context = auth.admin_context(request, db)
            query = select(NotificationEvent, User).outerjoin(
                User, NotificationEvent.user_id == User.id
            )
            if status in {"pending", "sending", "sent", "failed"}:
                query = query.where(NotificationEvent.status == status)
            else:
                status = "all"
            rows = list(db.execute(query.order_by(NotificationEvent.created_at.desc())).all())
            pages = max(1, (len(rows) + 5) // 6)
            current = min(max(1, page_number), pages)
            items = []
            service = NotificationService(db, pii, AuditService(db))
            for event, owner in rows[(current - 1) * 6:current * 6]:
                try:
                    recipient = mask_email(service.recipient_for(event))
                except Exception:
                    recipient = "无法解密"
                items.append((event, owner, recipient))
            paused = db.get(AppSetting, "email_paused")
            return page(
                request, "admin_notifications.html", title="邮件通知管理",
                items=items, page_number=current, pages=pages, status=status,
                email_paused=bool(paused and paused.value == "true"), **context,
            )

    @router.post("/admin/notifications/{event_id}/retry")
    def admin_retry_notification(
        request: Request, event_id: str, csrf_token: str = Form(default="")
    ):
        with session_scope(engine) as db:
            admin, record, _context = auth.admin_context(request, db)
            auth.csrf(record, csrf_token)
            try:
                NotificationService(db, pii, AuditService(db)).retry_failed(admin.id, event_id)
            except ValueError:
                raise HTTPException(409) from None
        return RedirectResponse("/admin/notifications#delivery-list", 303)

    @router.post("/admin/notifications/pause")
    def admin_pause_notifications(
        request: Request,
        paused: str = Form(default="false"),
        csrf_token: str = Form(default=""),
    ):
        with session_scope(engine) as db:
            admin, record, _context = auth.admin_context(request, db)
            auth.csrf(record, csrf_token)
            NotificationService(db, pii, AuditService(db)).set_paused(
                admin.id, paused == "true"
            )
        return RedirectResponse("/admin/notifications", 303)

    return router
