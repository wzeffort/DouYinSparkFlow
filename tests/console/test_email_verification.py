import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from sqlalchemy import select

from spark_console.config import Settings
from spark_console.db import create_engine_for, create_schema, session_scope
from spark_console.models import InviteCode, NotificationEvent, PendingRegistration, User
from spark_console.pii import PiiCipher
from spark_console.security import PasswordService
from spark_console.services import ValidationError
from spark_console.services.audits import AuditService
from spark_console.services.email_verification import EmailVerificationService
from spark_console.services.invites import InviteService


class EmailVerificationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        root = Path(self.temp.name)
        (root / "cookie.key").write_bytes(b"c" * 32)
        (root / "session.key").write_bytes(b"s" * 32)
        settings = Settings.from_env({
            "SPARK_DATA_DIR": str(root),
            "SPARK_COOKIE_KEY_FILE": str(root / "cookie.key"),
            "SPARK_SESSION_KEY_FILE": str(root / "session.key"),
        })
        self.engine = create_engine_for(settings)
        create_schema(self.engine)
        # Invite consumption checks the real clock, so keep this fixture safely
        # ahead of it instead of pinning the test to a calendar date.
        self.now = datetime.now(timezone.utc)
        self.pii = PiiCipher(b"p" * 32)
        self.passwords = PasswordService()
        with session_scope(self.engine) as db:
            admin = User(username="admin", password_hash="hash", role="admin")
            db.add(admin)
            db.flush()
            invite, self.code = InviteService(db, AuditService(db)).create(admin.id)
            invite.expires_at = self.now + timedelta(days=1)
            self.invite_id = invite.id

    def tearDown(self):
        self.engine.dispose()
        self.temp.cleanup()

    def service(self, db, code="123456"):
        return EmailVerificationService(
            db, self.passwords, self.pii, AuditService(db), code_factory=lambda: code
        )

    def test_invite_is_consumed_only_after_correct_code(self):
        with session_scope(self.engine) as db:
            pending = self.service(db).start_registration(
                "alice", "StrongPass123", "Alice@example.com", self.code, "198.51.100.2", self.now
            )
            pending_id = pending.id
            self.assertIsNone(db.get(InviteCode, self.invite_id).used_at)
            self.assertEqual(1, db.query(NotificationEvent).count())
        with self.assertRaises(ValidationError):
            with session_scope(self.engine) as db:
                self.service(db).verify_registration(pending_id, "000000", self.now)
        with session_scope(self.engine) as db:
            user = self.service(db).verify_registration(pending_id, "123456", self.now)
            self.assertEqual("alice", user.username)
            self.assertIsNotNone(user.email_verified_at)
            self.assertIsNotNone(db.get(InviteCode, self.invite_id).used_at)
            self.assertIsNone(db.get(PendingRegistration, pending_id))

    def test_code_expires_and_resend_invalidates_old_code(self):
        with session_scope(self.engine) as db:
            pending = self.service(db, "111111").start_registration(
                "alice", "StrongPass123", "alice@example.com", self.code, "client", self.now
            )
            pending_id = pending.id
        with session_scope(self.engine) as db:
            self.service(db, "222222").resend_registration(
                pending_id, "client", self.now + timedelta(seconds=61)
            )
        with self.assertRaises(ValidationError):
            with session_scope(self.engine) as db:
                self.service(db).verify_registration(pending_id, "111111", self.now + timedelta(seconds=62))
        with session_scope(self.engine) as db:
            user = self.service(db).verify_registration(
                pending_id, "222222", self.now + timedelta(seconds=62)
            )
            self.assertEqual("alice", user.username)


if __name__ == "__main__":
    unittest.main()
