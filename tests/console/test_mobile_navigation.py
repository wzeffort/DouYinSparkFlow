import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

from fastapi.testclient import TestClient

from spark_console.config import Settings
from spark_console.db import create_engine_for, create_schema, session_scope
from spark_console.models import User, UserNotification
from spark_console.security import PasswordService
from spark_console.web.app import create_app


ROOT = Path(__file__).resolve().parents[2]
HARNESS = ROOT / "tests" / "console" / "mobile_navigation_js_harness.js"


class MobileNavigationMarkupTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        root = Path(self.temp.name)
        cookie_key = root / "cookie.key"
        session_key = root / "session.key"
        pii_key = root / "pii.key"
        cookie_key.write_bytes(b"c" * 32)
        session_key.write_bytes(b"s" * 32)
        pii_key.write_bytes(b"p" * 32)
        settings = Settings(
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
        self.engine = create_engine_for(settings)
        create_schema(self.engine)
        with session_scope(self.engine) as db:
            user = User(
                username="alice",
                password_hash=PasswordService().hash("OriginalPass12"),
                must_change_password=False,
            )
            db.add(user)
            db.flush()
            db.add(
                UserNotification(
                    user_id=user.id,
                    kind="account",
                    title="账号提醒",
                    summary="需要处理",
                    dedupe_key="mobile-nav-fixture",
                )
            )
        self.client = TestClient(create_app(settings, self.engine), base_url="https://testserver")
        login = self.client.post(
            "/login",
            data={"username": "alice", "password": "OriginalPass12"},
            follow_redirects=False,
        )
        self.assertEqual(303, login.status_code)

    def tearDown(self):
        self.client.close()
        self.engine.dispose()
        self.temp.cleanup()

    def test_authenticated_page_renders_labeled_drawer_with_active_state_and_badge(self):
        response = self.client.get("/dashboard")

        self.assertEqual(200, response.status_code)
        self.assertIn('class="mobile-topbar"', response.text)
        self.assertIn('data-mobile-nav-open', response.text)
        self.assertIn('id="app-navigation"', response.text)
        self.assertIn('data-mobile-nav-close', response.text)
        self.assertIn('data-mobile-nav-scrim', response.text)
        self.assertIn('href="/dashboard" class="nav-link active"', response.text)
        for label in (
            "今日概览",
            "抖音账号",
            "续火任务",
            "执行记录",
            "通知中心",
            "邮箱与通知",
            "修改密码",
            "退出登录",
        ):
            self.assertIn(label, response.text)
        self.assertIn('class="nav-badge"', response.text)
        self.assertIn(">1</span>", response.text)
        self.assertGreaterEqual(response.text.count('class="nav-icon"'), 7)
        self.assertIn('/static/navigation.js', response.text)


@unittest.skipUnless(shutil.which("node"), "Node.js is required for navigation UI tests")
class MobileNavigationJavaScriptTests(unittest.TestCase):
    def test_drawer_opens_and_closes_with_controls_and_escape(self):
        completed = subprocess.run(
            [shutil.which("node"), str(HARNESS)],
            cwd=ROOT,
            capture_output=True,
            text=True,
            encoding="utf-8",
            check=False,
        )
        self.assertEqual(0, completed.returncode, completed.stderr)
        self.assertIn('"mobileNavigation":"ok"', completed.stdout)


if __name__ == "__main__":
    unittest.main()
