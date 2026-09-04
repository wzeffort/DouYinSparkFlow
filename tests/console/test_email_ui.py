import tempfile
import unittest
from pathlib import Path

from fastapi.testclient import TestClient

from spark_console.config import Settings
from spark_console.db import create_engine_for, create_schema, session_scope
from spark_console.models import User
from spark_console.security import PasswordService
from spark_console.web.app import create_app


class EmailSettingsUiTests(unittest.TestCase):
    def test_email_settings_uses_bounded_two_column_security_layout(self):
        template = Path("spark_console/templates/email_settings.html").read_text(
            encoding="utf-8"
        )
        css = Path("spark_console/static/app.css").read_text(encoding="utf-8")

        for marker in (
            'class="email-settings-page"',
            'class="email-hero"',
            'class="email-settings-grid"',
            'class="preference-option"',
            'class="preference-switch"',
            'class="email-bind-row"',
        ):
            self.assertIn(marker, template)
        self.assertIn("max-width:1040px", css)
        self.assertIn(".email-settings-grid", css)
        self.assertIn(".preference-switch", css)
        self.assertIn("@media(max-width:760px)", css)


class InlineEmailVerificationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        root = Path(self.temp.name)
        cookie_key = root / "cookie.key"
        session_key = root / "session.key"
        pii_key = root / "pii.key"
        cookie_key.write_bytes(b"c" * 32)
        session_key.write_bytes(b"s" * 32)
        pii_key.write_bytes(b"p" * 32)
        self.settings = Settings(
            data_dir=root,
            database_url=f"sqlite:///{root / 'test.db'}",
            cookie_key_file=cookie_key,
            session_key_file=session_key,
            pii_key_file=pii_key,
            public_base_url="https://example.com",
            email_enabled=True,
            resend_api_key="test-key",
            resend_from="Spark <notify@example.com>",
        )
        self.engine = create_engine_for(self.settings)
        create_schema(self.engine)
        with session_scope(self.engine) as db:
            db.add(
                User(
                    username="alice",
                    password_hash=PasswordService().hash("OriginalPass12"),
                    must_change_password=False,
                )
            )
        self.client = TestClient(
            create_app(self.settings, self.engine), base_url="https://testserver"
        )

    def tearDown(self):
        self.client.close()
        self.engine.dispose()
        self.temp.cleanup()

    def _login_and_csrf(self):
        response = self.client.post(
            "/login",
            data={"username": "alice", "password": "OriginalPass12"},
            follow_redirects=False,
        )
        self.assertEqual(303, response.status_code)
        page = self.client.get("/settings/email")
        marker = 'name="csrf_token" value="'
        return page.text.split(marker, 1)[1].split('"', 1)[0]

    def test_binding_code_expands_on_email_settings_page(self):
        csrf = self._login_and_csrf()

        response = self.client.post(
            "/settings/email/start",
            data={"csrf_token": csrf, "email": "alice@example.com"},
            follow_redirects=False,
        )

        self.assertEqual(303, response.status_code)
        self.assertTrue(response.headers["location"].startswith("/settings/email?verification="))
        expanded = self.client.get(response.headers["location"])
        self.assertEqual(200, expanded.status_code)
        self.assertIn('action="/settings/email/verify/', expanded.text)
        self.assertIn('autocomplete="one-time-code"', expanded.text)
        self.assertNotIn("EMAIL VERIFY", expanded.text)

    def test_password_reset_code_and_new_password_expand_on_request_page(self):
        response = self.client.post(
            "/forgot-password",
            data={"email": "unknown@example.com"},
            follow_redirects=False,
        )

        self.assertEqual(303, response.status_code)
        self.assertTrue(response.headers["location"].startswith("/forgot-password?verification="))
        expanded = self.client.get(response.headers["location"])
        self.assertEqual(200, expanded.status_code)
        self.assertIn('action="/forgot-password/verify/', expanded.text)
        self.assertIn('name="email"', expanded.text)
        self.assertIn('name="code"', expanded.text)
        self.assertIn('name="new_password"', expanded.text)
        self.assertIn('name="password_confirmation"', expanded.text)
        self.assertNotIn("下一页", expanded.text)


if __name__ == "__main__":
    unittest.main()
