import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from spark_console.config import Settings
from spark_console.db import create_engine_for, create_schema, session_scope
from spark_console.models import EmailActionToken, NotificationEvent, User
from spark_console.pii import PiiCipher
from spark_console.services.audits import AuditService
from spark_console.services.notifications import NotificationService


class NotificationServiceTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        root = Path(self.temp.name)
        (root / "cookie.key").write_bytes(b"c" * 32)
        (root / "session.key").write_bytes(b"s" * 32)
        self.engine = create_engine_for(Settings.from_env({
            "SPARK_DATA_DIR": str(root),
            "SPARK_COOKIE_KEY_FILE": str(root / "cookie.key"),
            "SPARK_SESSION_KEY_FILE": str(root / "session.key"),
        }))
        create_schema(self.engine)
        self.pii = PiiCipher(b"p" * 32)
        with session_scope(self.engine) as db:
            user = User(username="notify-user", password_hash="hash")
            db.add(user)
            db.flush()
            self.user_id = user.id

    def tearDown(self):
        self.engine.dispose()
        self.temp.cleanup()

    def service(self, db):
        return NotificationService(db, self.pii, AuditService(db))

    def test_duplicate_dedupe_creates_one_notice_and_event(self):
        with session_scope(self.engine) as db:
            service = self.service(db)
            first = service.create_in_app(
                self.user_id, "expired", "账号已过期", "请重新登录", "/accounts", "incident:1"
            )
            second = service.create_in_app(
                self.user_id, "expired", "账号已过期", "请重新登录", "/accounts", "incident:1"
            )
            self.assertEqual(first.id, second.id)
            event_a = service.enqueue_template(
                self.user_id, "expired", "user@example.com", "douyin_expired",
                {"account_name": "主账号", "action_path": "/accounts"}, "email:incident:1"
            )
            event_b = service.enqueue_template(
                self.user_id, "expired", "user@example.com", "douyin_expired",
                {"account_name": "主账号", "action_path": "/accounts"}, "email:incident:1"
            )
            self.assertEqual(event_a.id, event_b.id)
            self.assertEqual(1, db.query(NotificationEvent).count())

    def test_claim_retry_and_sent_state_machine(self):
        now = datetime(2026, 9, 1, tzinfo=timezone.utc)
        with session_scope(self.engine) as db:
            service = self.service(db)
            event = service.enqueue_template(
                self.user_id, "verify", "user@example.com", "verify_email",
                {"code": "123456"}, "verify:1", now=now
            )
            event_id = event.id
            self.assertNotIn("123456", event.payload_json)
            self.assertEqual({"code": "123456"}, service.payload_for(event))
        with session_scope(self.engine) as db:
            service = self.service(db)
            claimed = service.claim_due("worker-1", now)
            self.assertEqual(event_id, claimed.id)
            service.mark_failed(event_id, "provider_timeout", True, now)
            event = db.get(NotificationEvent, event_id)
            self.assertEqual("pending", event.status)
            self.assertEqual(now + timedelta(minutes=1), event.next_attempt_at.replace(tzinfo=timezone.utc))
        with session_scope(self.engine) as db:
            service = self.service(db)
            claimed = service.claim_due("worker-1", now + timedelta(minutes=1))
            service.mark_sent(claimed.id, "provider-id", now + timedelta(minutes=1))
            event = db.get(NotificationEvent, event_id)
            self.assertEqual("sent", event.status)
            self.assertEqual("{}", event.payload_json)

    def test_action_token_is_hashed_expiring_single_use_and_owner_scoped(self):
        now = datetime(2026, 9, 1, tzinfo=timezone.utc)
        with session_scope(self.engine) as db:
            other = User(username="other-user", password_hash="hash")
            db.add(other)
            db.flush()
            other_id = other.id
            service = self.service(db)
            token = service.create_action_token(self.user_id, "incident-1", now)
            row = db.query(EmailActionToken).one()
            self.assertNotEqual(token, row.token_hash)
            with self.assertRaises(ValueError):
                service.consume_action_token(other_id, token, now)
            service.consume_action_token(self.user_id, token, now)
            with self.assertRaises(ValueError):
                service.consume_action_token(self.user_id, token, now)
            expired = service.create_action_token(self.user_id, "incident-2", now)
            with self.assertRaises(ValueError):
                service.consume_action_token(self.user_id, expired, now + timedelta(minutes=31))


if __name__ == "__main__":
    unittest.main()
