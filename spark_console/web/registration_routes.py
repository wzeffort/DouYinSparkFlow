from __future__ import annotations

import ipaddress
from dataclasses import dataclass
from datetime import datetime, timezone
from urllib.parse import urlencode

from fastapi import APIRouter, Form, HTTPException, Request
from fastapi.responses import RedirectResponse
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from spark_console.db import session_scope
from spark_console.crypto import CookieCipher
from spark_console.rate_limit import FailedAttemptLimiter
from spark_console.security import PasswordService
from spark_console.services import Conflict, ValidationError
from spark_console.services.audits import AuditService
from spark_console.services.invites import InviteService
from spark_console.services.users import (
    UserService,
    validate_registration_password,
    validate_registration_username,
)
from spark_console.web.auth import WebAuth
from spark_console.models import InviteCode, SparkTask, TaskRun, User
from spark_console.pii import PiiCipher
from spark_console.services.email_verification import EmailVerificationService


PUBLIC_ERROR = "注册信息或邀请码无效"
FIELD_BY_MESSAGE = {
    "用户名须为 3–32 位字母、数字、下划线或短横线": "username",
    "该用户名不可用，请更换": "username",
    "请输入有效的邮箱地址": "email",
    "该邮箱不可用，请更换": "email",
    "密码至少需要 10 位": "password",
    "密码必须包含至少一个字母": "password",
    "密码必须包含至少一个数字": "password",
    "两次输入的密码不一致": "password_confirmation",
    "邀请码不存在，请检查后重试": "invite_code",
    "邀请码已被使用": "invite_code",
    "邀请码已被撤销": "invite_code",
    "邀请码已过期": "invite_code",
    "邀请码状态已变化，请重新提交": "invite_code",
}
_ADMIN_QUERY_KEYS = (
    "invite_page",
    "invite_status",
    "task_page",
    "task_q",
    "task_status",
)


def _admin_return(request: Request) -> str:
    values = {
        key: request.query_params.get(key, "")[:80]
        for key in _ADMIN_QUERY_KEYS
        if request.query_params.get(key)
    }
    query = urlencode(values)
    return f"/admin?{query}" if query else "/admin"


@dataclass(frozen=True)
class AdminInvite:
    invite: InviteCode
    status: str
    used_by_username: str | None
    can_revoke: bool
    code: str | None


def registration_client_key(request: Request) -> str:
    supplied = request.headers.get("x-real-ip")
    if supplied:
        try:
            return str(ipaddress.ip_address(supplied))
        except ValueError:
            pass
    return request.client.host if request.client is not None else "unknown"


def build_registration_router(
    engine,
    passwords: PasswordService,
    limiter: FailedAttemptLimiter,
    auth: WebAuth,
    page,
    cipher: CookieCipher,
    pii: PiiCipher | None = None,
    email_enabled: bool = False,
) -> APIRouter:
    router = APIRouter()

    @router.get("/register")
    def register_page(request: Request):
        return page(
            request, "register.html", title="注册", nav=False,
            email_enabled=email_enabled, field_errors={}, form_values={},
        )

    @router.post("/register")
    def register(
        request: Request,
        username: str = Form(default=""),
        password: str = Form(default=""),
        password_confirmation: str = Form(default=""),
        invite_code: str = Form(default=""),
        email: str = Form(default=""),
    ):
        key = registration_client_key(request)
        form_values = {
            "username": username.strip(),
            "email": email.strip(),
            "invite_code": invite_code.strip(),
        }
        if not limiter.allow(key):
            return page(
                request, "register.html", 429, title="注册", nav=False,
                error="尝试次数过多，请稍后再试", field_errors={},
                form_values=form_values, email_enabled=email_enabled,
            )
        try:
            with session_scope(engine) as db:
                validate_registration_username(username)
                validate_registration_password(password)
                if password != password_confirmation:
                    raise ValidationError("两次输入的密码不一致")
                if email_enabled:
                    if pii is None:
                        raise RuntimeError("email verification is not configured")
                    pending = EmailVerificationService(
                        db, passwords, pii, AuditService(db)
                    ).start_registration(
                        username, password, email, invite_code, key
                    )
                    pending_id = pending.id
                else:
                    user, _ = UserService(db, passwords, AuditService(db)).create(
                        username, password, "user"
                    )
                    user.must_change_password = False
                    InviteService(db, AuditService(db), cipher).consume(
                        invite_code, user.id
                    )
        except (ValidationError, Conflict, IntegrityError, ValueError) as exc:
            limiter.record_failure(key)
            message = str(exc)
            if isinstance(exc, ValueError) and message == "invalid email":
                message = "请输入有效的邮箱地址"
            field = FIELD_BY_MESSAGE.get(message)
            if field is None:
                message = "注册未完成，请检查填写内容后重试"
            return page(
                request, "register.html", 400, title="注册", nav=False,
                error=message, email_enabled=email_enabled,
                field_errors={field: message} if field else {},
                form_values=form_values,
            )
        limiter.clear(key)
        if email_enabled:
            return RedirectResponse(f"/register/verify/{pending_id}", 303)
        return RedirectResponse("/login?registered=1", 303)

    @router.get("/register/verify/{pending_id}")
    def verify_page(request: Request, pending_id: str):
        if not email_enabled:
            raise HTTPException(404)
        with session_scope(engine) as db:
            from spark_console.models import PendingRegistration
            pending = db.get(PendingRegistration, pending_id)
            if pending is None:
                raise HTTPException(404)
            username = pending.username
        return page(
            request, "register_verify.html", title="验证邮箱", nav=False,
            pending_id=pending_id, username=username,
        )

    @router.post("/register/verify/{pending_id}")
    def verify_registration(
        request: Request, pending_id: str, code: str = Form(default="")
    ):
        if not email_enabled or pii is None:
            raise HTTPException(404)
        error = None
        with session_scope(engine) as db:
            try:
                EmailVerificationService(
                    db, passwords, pii, AuditService(db)
                ).verify_registration(pending_id, code)
            except (ValidationError, Conflict, IntegrityError, ValueError):
                error = "验证码无效或已过期"
        if error:
            return page(
                request, "register_verify.html", 400, title="验证邮箱", nav=False,
                pending_id=pending_id, username="", error=error,
            )
        return RedirectResponse("/login?registered=1", 303)

    @router.post("/register/verify/{pending_id}/resend")
    def resend_registration(request: Request, pending_id: str):
        if not email_enabled or pii is None:
            raise HTTPException(404)
        error = None
        with session_scope(engine) as db:
            try:
                pending = EmailVerificationService(
                    db, passwords, pii, AuditService(db)
                ).resend_registration(pending_id, registration_client_key(request))
                username = pending.username
            except (ValidationError, ValueError) as exc:
                error = str(exc)
                username = ""
        return page(
            request, "register_verify.html", 400 if error else 200,
            title="验证邮箱", nav=False, pending_id=pending_id,
            username=username, error=error,
            notice=None if error else "验证码已重新发送",
        )

    @router.post("/admin/invites")
    def create_invite(request: Request, csrf_token: str = Form(default="")):
        with session_scope(engine) as db:
            admin, record, _context = auth.admin_context(request, db)
            auth.csrf(record, csrf_token)
            InviteService(db, AuditService(db), cipher).create(admin.id)
        return RedirectResponse(_admin_return(request), 303)

    @router.post("/admin/invites/{invite_id}/revoke")
    def revoke_invite(
        request: Request, invite_id: str, csrf_token: str = Form(default="")
    ):
        with session_scope(engine) as db:
            admin, record, _context = auth.admin_context(request, db)
            auth.csrf(record, csrf_token)
            try:
                InviteService(db, AuditService(db), cipher).revoke(
                    admin.id, invite_id
                )
            except ValidationError as error:
                raise HTTPException(400, str(error)) from error
        return RedirectResponse(_admin_return(request), 303)

    @router.post("/admin/invites/{invite_id}/delete")
    def delete_invite(
        request: Request, invite_id: str, csrf_token: str = Form(default="")
    ):
        with session_scope(engine) as db:
            admin, record, _context = auth.admin_context(request, db)
            auth.csrf(record, csrf_token)
            try:
                InviteService(db, AuditService(db), cipher).delete(
                    admin.id, invite_id
                )
            except ValidationError as error:
                raise HTTPException(404, "邀请码不存在") from error
        return RedirectResponse(_admin_return(request), 303)

    return router


def _admin_page_data(db):
    users = db.scalars(select(User).order_by(User.created_at)).all()
    tasks = db.execute(
        select(SparkTask, User)
        .join(User, SparkTask.owner_user_id == User.id)
        .order_by(SparkTask.send_time)
    ).all()
    runs = db.execute(
        select(TaskRun, SparkTask, User)
        .join(SparkTask, TaskRun.task_id == SparkTask.id)
        .join(User, SparkTask.owner_user_id == User.id)
        .order_by(TaskRun.scheduled_for.desc())
        .limit(20)
    ).all()
    return users, tasks, runs


def admin_invite_items(
    db, cipher: CookieCipher | None = None
) -> list[AdminInvite]:
    now = datetime.now(timezone.utc)
    rows = db.execute(
        select(InviteCode, User.username)
        .outerjoin(User, InviteCode.used_by_user_id == User.id)
        .order_by(InviteCode.created_at.desc())
    ).all()
    items = []
    service = InviteService(db, AuditService(db), cipher)
    for invite, username in rows:
        expires_at = invite.expires_at
        if expires_at.tzinfo is None:
            expires_at = expires_at.replace(tzinfo=timezone.utc)
        if invite.used_at is not None:
            status = "已使用"
        elif invite.revoked_at is not None:
            status = "已撤销"
        elif expires_at <= now:
            status = "已过期"
        else:
            status = "有效"
        items.append(
            AdminInvite(
                invite=invite,
                status=status,
                used_by_username=username,
                can_revoke=status == "有效",
                code=service.reveal(invite.id),
            )
        )
    return items
