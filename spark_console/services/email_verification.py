from __future__ import annotations

import hashlib
import secrets
from datetime import datetime, timedelta, timezone

from sqlalchemy import delete, func, select
from sqlalchemy.orm import Session

from spark_console.models import (
    EmailVerificationRequest,
    InviteCode,
    NotificationPreference,
    PendingRegistration,
    User,
    utc_now,
)
from spark_console.pii import PiiCipher, normalize_email
from spark_console.security import PasswordService
from spark_console.services import Conflict, ValidationError
from spark_console.services.audits import AuditService
from spark_console.services.invites import InviteService
from spark_console.services.notifications import NotificationService
from spark_console.services.task_capacity import TaskCapacityService
from spark_console.services.users import (
    validate_registration_password,
    validate_registration_username,
)


PUBLIC_ERROR = "注册信息或邀请码无效"


def _aware(value: datetime) -> datetime:
    return value if value.tzinfo else value.replace(tzinfo=timezone.utc)


class EmailVerificationService:
    CODE_LIFETIME = timedelta(minutes=10)
    PENDING_LIFETIME = timedelta(minutes=30)
    RESEND_COOLDOWN = timedelta(seconds=60)
    MAX_ATTEMPTS = 5

    def __init__(
        self,
        session: Session,
        passwords: PasswordService,
        pii: PiiCipher,
        audit: AuditService,
        code_factory=None,
    ):
        self.session = session
        self.passwords = passwords
        self.pii = pii
        self.audit = audit
        self.code_factory = code_factory or (lambda: f"{secrets.randbelow(1_000_000):06d}")

    @staticmethod
    def _username(value: str) -> str:
        return validate_registration_username(value)

    @staticmethod
    def _invite_hash(code: str) -> str:
        return hashlib.sha256(code.strip().encode("utf-8")).hexdigest()

    def start_registration(
        self,
        username: str,
        password: str,
        email: str,
        invite_code: str,
        client_key: str,
        now: datetime | None = None,
    ) -> PendingRegistration:
        current = now or utc_now()
        name = self._username(username)
        validate_registration_password(password)
        normalized = normalize_email(email)
        lookup = self.pii.lookup_hash(normalized)
        if self.session.scalar(select(User.id).where(User.username == name)):
            raise Conflict("该用户名不可用，请更换")
        if self.session.scalar(select(User.id).where(User.email_lookup_hash == lookup)):
            raise Conflict("该邮箱不可用，请更换")
        invite = InviteService(self.session, self.audit).for_registration(invite_code)
        recent = self.session.scalar(
            select(func.count(PendingRegistration.id)).where(
                PendingRegistration.email_lookup_hash == lookup,
                PendingRegistration.created_at >= current - timedelta(hours=1),
            )
        ) or 0
        if recent >= 5:
            raise ValidationError("验证码发送过于频繁，请稍后再试")
        pending_id = secrets.token_hex(16)
        encrypted, nonce = self.pii.encrypt_email(
            normalized, aad=f"pending:{pending_id}".encode()
        )
        code = self.code_factory()
        pending = PendingRegistration(
            id=pending_id,
            username=name,
            password_hash=self.passwords.hash(password),
            invite_id=invite.id,
            email_ciphertext=encrypted,
            email_nonce=nonce,
            email_lookup_hash=lookup,
            code_hash=self.pii.code_hash(f"register:{pending_id}", code),
            client_key_hash=hashlib.sha256(client_key.encode()).hexdigest(),
            code_expires_at=current + self.CODE_LIFETIME,
            resend_available_at=current + self.RESEND_COOLDOWN,
            expires_at=current + self.PENDING_LIFETIME,
            created_at=current,
        )
        self.session.add(pending)
        self.session.flush()
        NotificationService(self.session, self.pii, self.audit).enqueue_template(
            None,
            "email_verification",
            normalized,
            "verify_email",
            {"code": code, "username": name},
            f"register:{pending.id}:1",
            now=current,
        )
        return pending

    def verify_registration(
        self, pending_id: str, code: str, now: datetime | None = None
    ) -> User:
        current = now or utc_now()
        pending = self.session.get(PendingRegistration, pending_id)
        if (
            pending is None
            or _aware(pending.expires_at) <= _aware(current)
            or _aware(pending.code_expires_at) <= _aware(current)
            or pending.failed_attempts >= self.MAX_ATTEMPTS
        ):
            raise ValidationError("验证码无效或已过期")
        if not self.pii.verify_code(f"register:{pending.id}", code.strip(), pending.code_hash):
            pending.failed_attempts += 1
            raise ValidationError("验证码无效或已过期")
        if self.session.scalar(select(User.id).where(User.username == pending.username)):
            raise Conflict(PUBLIC_ERROR)
        if self.session.scalar(
            select(User.id).where(User.email_lookup_hash == pending.email_lookup_hash)
        ):
            raise Conflict(PUBLIC_ERROR)
        email = self.pii.decrypt_email(
            pending.email_ciphertext,
            pending.email_nonce,
            aad=f"pending:{pending.id}".encode(),
        )
        user_id = secrets.token_hex(16)
        ciphertext, nonce = self.pii.encrypt_email(email, aad=f"user:{user_id}".encode())
        user = User(
            id=user_id,
            username=pending.username,
            password_hash=pending.password_hash,
            role="user",
            must_change_password=False,
            email_ciphertext=ciphertext,
            email_nonce=nonce,
            email_lookup_hash=pending.email_lookup_hash,
            email_verified_at=current,
            email_updated_at=current,
        )
        self.session.add(user)
        self.session.flush()
        InviteService(self.session, self.audit).consume_id(pending.invite_id, user.id)
        TaskCapacityService(self.session, self.audit).bootstrap_user(user, use_current_policy=True)
        self.session.add(NotificationPreference(user_id=user.id))
        self.audit.write(user.id, "user.registered", "user", user.id)
        self.session.delete(pending)
        return user

    def resend_registration(
        self, pending_id: str, client_key: str, now: datetime | None = None
    ) -> PendingRegistration:
        current = now or utc_now()
        pending = self.session.get(PendingRegistration, pending_id)
        if pending is None or _aware(pending.expires_at) <= _aware(current):
            raise ValidationError("验证请求已过期，请重新注册")
        if _aware(pending.resend_available_at) > _aware(current):
            raise ValidationError("请稍后再重新发送")
        if pending.send_count >= 5:
            raise ValidationError("验证码发送过于频繁，请稍后再试")
        if pending.client_key_hash != hashlib.sha256(client_key.encode()).hexdigest():
            raise ValidationError("验证请求无效")
        email = self.pii.decrypt_email(
            pending.email_ciphertext,
            pending.email_nonce,
            aad=f"pending:{pending.id}".encode(),
        )
        code = self.code_factory()
        pending.send_count += 1
        pending.failed_attempts = 0
        pending.code_hash = self.pii.code_hash(f"register:{pending.id}", code)
        pending.code_expires_at = current + self.CODE_LIFETIME
        pending.resend_available_at = current + self.RESEND_COOLDOWN
        NotificationService(self.session, self.pii, self.audit).enqueue_template(
            None,
            "email_verification",
            email,
            "verify_email",
            {"code": code, "username": pending.username},
            f"register:{pending.id}:{pending.send_count}",
            now=current,
        )
        return pending

    def cleanup_expired(self, now: datetime | None = None) -> int:
        current = now or utc_now()
        pending = self.session.execute(
            delete(PendingRegistration).where(PendingRegistration.expires_at <= current)
        ).rowcount
        requests = self.session.execute(
            delete(EmailVerificationRequest).where(
                (EmailVerificationRequest.expires_at <= current)
                | (EmailVerificationRequest.consumed_at.is_not(None))
            )
        ).rowcount
        return int(pending or 0) + int(requests or 0)

    def start_binding(
        self, user_id: str, email: str, now: datetime | None = None
    ) -> EmailVerificationRequest:
        current = now or utc_now()
        user = self.session.get(User, user_id)
        if user is None:
            raise ValidationError("用户不存在")
        normalized = normalize_email(email)
        lookup = self.pii.lookup_hash(normalized)
        duplicate = self.session.scalar(
            select(User.id).where(
                User.email_lookup_hash == lookup,
                User.id != user_id,
            )
        )
        if duplicate:
            raise Conflict("该邮箱已绑定其他账号")
        self.session.query(EmailVerificationRequest).filter(
            EmailVerificationRequest.user_id == user_id,
            EmailVerificationRequest.consumed_at.is_(None),
        ).update({EmailVerificationRequest.consumed_at: current})
        request_id = secrets.token_hex(16)
        ciphertext, nonce = self.pii.encrypt_email(
            normalized, aad=f"email-request:{request_id}".encode()
        )
        code = self.code_factory()
        request = EmailVerificationRequest(
            id=request_id,
            user_id=user_id,
            purpose="bind",
            email_ciphertext=ciphertext,
            email_nonce=nonce,
            email_lookup_hash=lookup,
            code_hash=self.pii.code_hash(f"bind:{request_id}", code),
            code_expires_at=current + self.CODE_LIFETIME,
            resend_available_at=current + self.RESEND_COOLDOWN,
            expires_at=current + self.PENDING_LIFETIME,
            created_at=current,
        )
        self.session.add(request)
        self.session.flush()
        NotificationService(self.session, self.pii, self.audit).enqueue_template(
            user_id,
            "email_verification",
            normalized,
            "verify_email",
            {"code": code, "username": user.username},
            f"bind:{request.id}:1",
            now=current,
        )
        return request

    def verify_binding(
        self,
        user_id: str,
        request_id: str,
        code: str,
        now: datetime | None = None,
    ) -> User:
        current = now or utc_now()
        request = self.session.get(EmailVerificationRequest, request_id)
        if (
            request is None
            or request.user_id != user_id
            or request.consumed_at is not None
            or _aware(request.expires_at) <= _aware(current)
            or _aware(request.code_expires_at) <= _aware(current)
            or request.failed_attempts >= self.MAX_ATTEMPTS
        ):
            raise ValidationError("验证码无效或已过期")
        if not self.pii.verify_code(f"bind:{request.id}", code.strip(), request.code_hash):
            request.failed_attempts += 1
            raise ValidationError("验证码无效或已过期")
        duplicate = self.session.scalar(
            select(User.id).where(
                User.email_lookup_hash == request.email_lookup_hash,
                User.id != user_id,
            )
        )
        if duplicate:
            raise Conflict("该邮箱已绑定其他账号")
        email = self.pii.decrypt_email(
            request.email_ciphertext,
            request.email_nonce,
            aad=f"email-request:{request.id}".encode(),
        )
        user = self.session.get(User, user_id)
        ciphertext, nonce = self.pii.encrypt_email(
            email, aad=f"user:{user.id}".encode()
        )
        user.email_ciphertext = ciphertext
        user.email_nonce = nonce
        user.email_lookup_hash = request.email_lookup_hash
        user.email_verified_at = current
        user.email_updated_at = current
        request.consumed_at = current
        if self.session.get(NotificationPreference, user.id) is None:
            self.session.add(NotificationPreference(user_id=user.id))
        self.audit.write(user.id, "email.verified", "user", user.id)
        return user

    def email_for_user(self, user: User) -> str | None:
        if not user.email_ciphertext or not user.email_nonce:
            return None
        return self.pii.decrypt_email(
            user.email_ciphertext,
            user.email_nonce,
            aad=f"user:{user.id}".encode(),
        )

    def start_password_reset(
        self, email: str, client_key: str, now: datetime | None = None
    ) -> EmailVerificationRequest | None:
        current = now or utc_now()
        try:
            normalized = normalize_email(email)
        except ValueError:
            return None
        lookup = self.pii.lookup_hash(normalized)
        user = self.session.scalar(
            select(User).where(
                User.email_lookup_hash == lookup,
                User.email_verified_at.is_not(None),
                User.status == "active",
            )
        )
        if user is None:
            return None
        recent = self.session.scalar(
            select(func.count(EmailVerificationRequest.id)).where(
                EmailVerificationRequest.email_lookup_hash == lookup,
                EmailVerificationRequest.purpose == "password_reset",
                EmailVerificationRequest.created_at >= current - timedelta(hours=1),
            )
        ) or 0
        if recent >= 5:
            return None
        self.session.query(EmailVerificationRequest).filter(
            EmailVerificationRequest.user_id == user.id,
            EmailVerificationRequest.purpose == "password_reset",
            EmailVerificationRequest.consumed_at.is_(None),
        ).update({EmailVerificationRequest.consumed_at: current})
        request_id = secrets.token_hex(16)
        encrypted, nonce = self.pii.encrypt_email(
            normalized, aad=f"email-request:{request_id}".encode()
        )
        code = self.code_factory()
        request = EmailVerificationRequest(
            id=request_id,
            user_id=user.id,
            purpose="password_reset",
            email_ciphertext=encrypted,
            email_nonce=nonce,
            email_lookup_hash=lookup,
            code_hash=self.pii.code_hash(f"password-reset:{request_id}", code),
            code_expires_at=current + self.CODE_LIFETIME,
            resend_available_at=current + self.RESEND_COOLDOWN,
            expires_at=current + self.PENDING_LIFETIME,
            created_at=current,
        )
        self.session.add(request)
        self.session.flush()
        NotificationService(self.session, self.pii, self.audit).enqueue_template(
            user.id,
            "password_reset",
            normalized,
            "reset_password",
            {"code": code, "username": user.username},
            f"password-reset:{request.id}:1",
            now=current,
        )
        self.audit.write(user.id, "password.reset_requested", "user", user.id)
        return request

    def complete_password_reset(
        self,
        request_id: str,
        code: str,
        new_password: str,
        now: datetime | None = None,
    ) -> User:
        from spark_console.models import WebSession

        current = now or utc_now()
        request = self.session.get(EmailVerificationRequest, request_id)
        if (
            request is None
            or request.purpose != "password_reset"
            or request.consumed_at is not None
            or _aware(request.expires_at) <= _aware(current)
            or _aware(request.code_expires_at) <= _aware(current)
            or request.failed_attempts >= self.MAX_ATTEMPTS
        ):
            raise ValidationError("验证码无效或已过期")
        if not self.pii.verify_code(
            f"password-reset:{request.id}", code.strip(), request.code_hash
        ):
            request.failed_attempts += 1
            raise ValidationError("验证码无效或已过期")
        validate_registration_password(new_password)
        user = self.session.get(User, request.user_id)
        if user is None or user.status != "active":
            raise ValidationError("验证码无效或已过期")
        user.password_hash = self.passwords.hash(new_password)
        user.must_change_password = False
        user.failed_login_count = 0
        user.locked_until = None
        request.consumed_at = current
        self.session.query(WebSession).filter(WebSession.user_id == user.id).delete()
        self.audit.write(user.id, "password.reset_completed", "user", user.id)
        return user
