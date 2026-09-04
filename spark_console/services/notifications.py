from __future__ import annotations

import base64
import json
import secrets
from datetime import datetime, timedelta, timezone

from sqlalchemy import select
from sqlalchemy.orm import Session

from spark_console.models import (
    AppSetting,
    EmailActionToken,
    NotificationEvent,
    UserNotification,
    utc_now,
    uuid_string,
)
from spark_console.pii import PiiCipher
from spark_console.services.audits import AuditService


TEMPLATE_FIELDS = {
    "verify_email": {"code", "username"},
    "reset_password": {"code", "username"},
    "douyin_expired": {"account_name", "action_path"},
    "task_failure": {"target_name", "reason", "action_path"},
}


def _aware(value: datetime) -> datetime:
    return value if value.tzinfo else value.replace(tzinfo=timezone.utc)


class NotificationService:
    RETRY_DELAYS = (
        timedelta(minutes=1),
        timedelta(minutes=5),
        timedelta(minutes=20),
        timedelta(minutes=60),
    )

    def __init__(self, session: Session, pii: PiiCipher, audit: AuditService):
        self.session = session
        self.pii = pii
        self.audit = audit

    @staticmethod
    def _action_path(value: str | None) -> str | None:
        if value is None:
            return None
        if not value.startswith("/") or value.startswith("//") or len(value) > 240:
            raise ValueError("action path must be a safe relative path")
        return value

    def create_in_app(
        self,
        user_id: str,
        kind: str,
        title: str,
        summary: str,
        action_path: str | None,
        dedupe_key: str,
    ) -> UserNotification:
        existing = self.session.scalar(
            select(UserNotification).where(UserNotification.dedupe_key == dedupe_key)
        )
        if existing is not None:
            return existing
        notice = UserNotification(
            user_id=user_id,
            kind=kind[:48],
            title=title[:120],
            summary=summary[:240],
            action_path=self._action_path(action_path),
            dedupe_key=dedupe_key[:160],
        )
        self.session.add(notice)
        self.session.flush()
        return notice

    def enqueue_template(
        self,
        user_id: str | None,
        kind: str,
        recipient: str,
        template_key: str,
        payload: dict[str, str],
        dedupe_key: str,
        *,
        now: datetime | None = None,
    ) -> NotificationEvent:
        existing = self.session.scalar(
            select(NotificationEvent).where(NotificationEvent.dedupe_key == dedupe_key)
        )
        if existing is not None:
            return existing
        allowed = TEMPLATE_FIELDS.get(template_key)
        if allowed is None or set(payload) - allowed:
            raise ValueError("unsupported email template payload")
        if "action_path" in payload:
            payload = dict(payload)
            payload["action_path"] = self._action_path(payload["action_path"]) or ""
        event_id = uuid_string()
        encrypted, nonce = self.pii.encrypt_email(
            recipient, aad=f"notification:{event_id}".encode()
        )
        current = now or utc_now()
        payload_bytes = json.dumps(
            payload, ensure_ascii=False, separators=(",", ":")
        ).encode("utf-8")
        payload_ciphertext, payload_nonce = self.pii.encrypt_bytes(
            payload_bytes, aad=f"notification-payload:{event_id}".encode()
        )
        sealed_payload = json.dumps(
            {
                "ciphertext": base64.urlsafe_b64encode(payload_ciphertext).decode("ascii"),
                "nonce": base64.urlsafe_b64encode(payload_nonce).decode("ascii"),
            },
            separators=(",", ":"),
        )
        event = NotificationEvent(
            id=event_id,
            user_id=user_id,
            kind=kind[:48],
            recipient_ciphertext=encrypted,
            recipient_nonce=nonce,
            template_key=template_key,
            payload_json=sealed_payload,
            dedupe_key=dedupe_key[:160],
            next_attempt_at=current,
        )
        self.session.add(event)
        self.session.flush()
        return event

    def payload_for(self, event: NotificationEvent) -> dict[str, str]:
        envelope = json.loads(event.payload_json)
        plaintext = self.pii.decrypt_bytes(
            base64.urlsafe_b64decode(envelope["ciphertext"]),
            base64.urlsafe_b64decode(envelope["nonce"]),
            aad=f"notification-payload:{event.id}".encode(),
        )
        payload = json.loads(plaintext)
        if not isinstance(payload, dict):
            raise ValueError("invalid notification payload")
        return {str(key): str(value) for key, value in payload.items()}

    def recipient_for(self, event: NotificationEvent) -> str:
        return self.pii.decrypt_email(
            event.recipient_ciphertext,
            event.recipient_nonce,
            aad=f"notification:{event.id}".encode(),
        )

    def claim_due(self, worker_id: str, now: datetime | None = None) -> NotificationEvent | None:
        current = now or utc_now()
        paused = self.session.get(AppSetting, "email_paused")
        if paused is not None and paused.value == "true":
            return None
        event = self.session.scalar(
            select(NotificationEvent)
            .where(
                NotificationEvent.status == "pending",
                NotificationEvent.next_attempt_at <= current,
            )
            .order_by(NotificationEvent.next_attempt_at, NotificationEvent.created_at)
            .limit(1)
        )
        if event is None:
            return None
        event.status = "sending"
        event.worker_id = worker_id[:64]
        event.claimed_at = current
        event.attempt_count += 1
        self.session.flush()
        return event

    def mark_sent(self, event_id: str, provider_id: str, now: datetime | None = None) -> None:
        event = self.session.get(NotificationEvent, event_id)
        if event is None or event.status != "sending":
            raise ValueError("notification is not sending")
        event.status = "sent"
        event.provider_id = provider_id[:128]
        event.payload_json = "{}"
        event.error_code = None
        event.sent_at = now or utc_now()
        event.worker_id = None
        event.claimed_at = None

    def mark_failed(
        self,
        event_id: str,
        error_code: str,
        retryable: bool,
        now: datetime | None = None,
    ) -> None:
        event = self.session.get(NotificationEvent, event_id)
        if event is None or event.status != "sending":
            raise ValueError("notification is not sending")
        current = now or utc_now()
        event.error_code = error_code[:48]
        event.worker_id = None
        event.claimed_at = None
        retry_index = event.attempt_count - 1
        if retryable and retry_index < len(self.RETRY_DELAYS):
            event.status = "pending"
            event.next_attempt_at = current + self.RETRY_DELAYS[retry_index]
        else:
            event.status = "failed"

    def recover_stale(self, now: datetime | None = None) -> int:
        current = now or utc_now()
        rows = self.session.scalars(
            select(NotificationEvent).where(
                NotificationEvent.status == "sending",
                NotificationEvent.claimed_at < current - timedelta(minutes=5),
            )
        ).all()
        for event in rows:
            event.status = "pending"
            event.worker_id = None
            event.claimed_at = None
            event.next_attempt_at = current
        return len(rows)

    def retry_failed(self, actor_id: str, event_id: str, now: datetime | None = None) -> None:
        event = self.session.get(NotificationEvent, event_id)
        if event is None or event.status != "failed":
            raise ValueError("only failed notifications can be retried")
        event.status = "pending"
        event.next_attempt_at = now or utc_now()
        event.error_code = None
        self.audit.write(actor_id, "notification.retried", "notification_event", event.id)

    def set_paused(self, actor_id: str, paused: bool) -> None:
        setting = self.session.get(AppSetting, "email_paused")
        if setting is None:
            setting = AppSetting(key="email_paused", value="false")
            self.session.add(setting)
        setting.value = "true" if paused else "false"
        self.audit.write(actor_id, "email.paused" if paused else "email.resumed", "app_setting", "email_paused")

    def create_action_token(self, user_id: str, incident_id: str, now: datetime | None = None) -> str:
        current = now or utc_now()
        token = secrets.token_urlsafe(32)
        row = EmailActionToken(
            user_id=user_id,
            incident_id=incident_id,
            token_hash=self.pii.token_hash(f"user:{user_id}", token),
            expires_at=current + timedelta(minutes=30),
        )
        self.session.add(row)
        self.session.flush()
        return token

    def consume_action_token(
        self, user_id: str, plaintext_token: str, now: datetime | None = None
    ) -> EmailActionToken:
        current = now or utc_now()
        digest = self.pii.token_hash(f"user:{user_id}", plaintext_token)
        row = self.session.scalar(
            select(EmailActionToken).where(EmailActionToken.token_hash == digest)
        )
        if (
            row is None
            or row.user_id != user_id
            or row.consumed_at is not None
            or _aware(row.expires_at) <= _aware(current)
        ):
            raise ValueError("invalid or expired action token")
        row.consumed_at = current
        return row
