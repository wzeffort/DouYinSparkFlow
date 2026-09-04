import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

from spark_console.config import Settings
from spark_console.db import create_engine_for, create_schema, session_scope
from spark_console.models import NotificationEvent, User, WebSession
from spark_console.pii import PiiCipher
from spark_console.security import PasswordService, SessionService
from spark_console.services import ValidationError
from spark_console.services.audits import AuditService
from spark_console.services.email_verification import EmailVerificationService


class PasswordResetTests(unittest.TestCase):
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
        self.pii = PiiCipher(b"p" * 32)
        self.passwords = PasswordService()
        self.now = datetime(2026, 9, 1, tzinfo=timezone.utc)
        with session_scope(self.engine) as db:
            user = User(
                username="alice",
                password_hash=self.passwords.hash("OriginalPass12"),
                email_verified_at=self.now,
            )
            db.add(user)
            db.flush()
            ciphertext, nonce = self.pii.encrypt_email(
                "alice@example.com", aad=f"user:{user.id}".encode()
            )
            user.email_ciphertext = ciphertext
            user.email_nonce = nonce
            user.email_lookup_hash = self.pii.lookup_hash("alice@example.com")
            raw, web_session = SessionService(b"s" * 32).create_record(user.id, self.now)
            db.add(web_session)
            self.user_id = user.id

    def tearDown(self):
        self.engine.dispose()
        self.temp.cleanup()

    def service(self, db):
        return EmailVerificationService(
            db, self.passwords, self.pii, AuditService(db), code_factory=lambda: "314159"
        )

    def test_verified_email_can_reset_password_and_revokes_old_sessions(self):
        with session_scope(self.engine) as db:
            request = self.service(db).start_password_reset(
                "alice@example.com", "198.51.100.1", self.now
            )
            self.assertIsNotNone(request)
            request_id = request.id
            self.assertEqual(1, db.query(NotificationEvent).count())
        with session_scope(self.engine) as db:
            user = self.service(db).complete_password_reset(
                request_id, "314159", "ChangedPass12", self.now
            )
            self.assertTrue(self.passwords.verify(user.password_hash, "ChangedPass12"))
            self.assertEqual(0, db.query(WebSession).filter(WebSession.user_id == self.user_id).count())

    def test_unknown_email_creates_no_event_and_wrong_code_is_rejected(self):
        with session_scope(self.engine) as db:
            self.assertIsNone(
                self.service(db).start_password_reset(
                    "unknown@example.com", "198.51.100.1", self.now
                )
            )
            self.assertEqual(0, db.query(NotificationEvent).count())
            request = self.service(db).start_password_reset(
                "alice@example.com", "198.51.100.1", self.now
            )
            request_id = request.id
        with self.assertRaises(ValidationError):
            with session_scope(self.engine) as db:
                self.service(db).complete_password_reset(
                    request_id, "000000", "ChangedPass12", self.now
                )


if __name__ == "__main__":
    unittest.main()
