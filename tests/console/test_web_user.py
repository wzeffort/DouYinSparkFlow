import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path

from fastapi.testclient import TestClient
from sqlalchemy import create_engine, select

from spark_console.config import Settings
from spark_console.crypto import CookieCipher
from spark_console.db import create_schema, session_scope
from spark_console.models import (
    DouyinAccount,
    DouyinContactIdentity,
    DouyinConversation,
    SparkTask,
    SparkTaskTargetIdentity,
    TaskQuotaGrant,
    TaskQuotaPolicy,
    TaskRun,
    User,
    WorkerLock,
)
from spark_console.security import PasswordService
from spark_console.services.accounts import AccountService
from spark_console.services.audits import AuditService
from spark_console.web.app import create_app


class UserWebTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        root = Path(self.temp.name)
        self.settings = Settings(
            data_dir=root,
            database_url=f"sqlite:///{root / 'test.db'}",
            cookie_key_file=root / "cookie.key",
            session_key_file=root / "session.key",
        )
        self.settings.cookie_key_file.write_bytes(b"c" * 32)
        self.settings.session_key_file.write_bytes(b"s" * 32)
        self.engine = create_engine(
            self.settings.database_url, connect_args={"check_same_thread": False}
        )
        create_schema(self.engine)
        with session_scope(self.engine) as session:
            session.add(
                User(
                    username="friend",
                    password_hash=PasswordService().hash("Temporary-123!"),
                    role="user",
                    must_change_password=True,
                )
            )
        self.client = TestClient(
            create_app(self.settings, self.engine), base_url="https://testserver"
        )

    def tearDown(self):
        self.client.close()
        self.engine.dispose()
        self.temp.cleanup()

    def login(self):
        return self.client.post(
            "/login",
            data={"username": "friend", "password": "Temporary-123!"},
            follow_redirects=False,
        )

    def test_first_login_is_forced_to_change_password(self):
        response = self.login()
        self.assertEqual(303, response.status_code)
        self.assertEqual("/change-password", response.headers["location"])
        dashboard = self.client.get("/dashboard", follow_redirects=False)
        self.assertEqual(303, dashboard.status_code)
        self.assertEqual("/change-password", dashboard.headers["location"])

    def test_post_without_csrf_is_rejected(self):
        self.login()
        response = self.client.post(
            "/change-password",
            data={"current_password": "Temporary-123!", "new_password": "Permanent-123!"},
        )
        self.assertEqual(403, response.status_code)

    def test_logged_in_user_has_change_password_navigation(self):
        self.login()
        page = self.client.get("/change-password")
        marker = 'name="csrf_token" value="'
        csrf = page.text.split(marker, 1)[1].split('"', 1)[0]
        self.client.post(
            "/change-password",
            data={
                "csrf_token": csrf,
                "current_password": "Temporary-123!",
                "new_password": "Permanent-123!",
                "new_password_confirmation": "Permanent-123!",
            },
        )

        dashboard = self.client.get("/dashboard")
        change_page = self.client.get("/change-password")

        self.assertIn('href="/change-password"', dashboard.text)
        self.assertIn("修改密码", change_page.text)
        self.assertIn('name="new_password_confirmation"', change_page.text)

    def test_change_password_page_uses_centered_security_layout(self):
        self.login()

        page = self.client.get("/change-password")

        self.assertEqual(200, page.status_code)
        self.assertIn('class="security-page"', page.text)
        self.assertIn('class="auth-card security-card"', page.text)

    def test_change_password_rejects_mismatched_confirmation(self):
        self.login()
        page = self.client.get("/change-password")
        marker = 'name="csrf_token" value="'
        csrf = page.text.split(marker, 1)[1].split('"', 1)[0]

        response = self.client.post(
            "/change-password",
            data={
                "csrf_token": csrf,
                "current_password": "Temporary-123!",
                "new_password": "Permanent-123!",
                "new_password_confirmation": "Different-123!",
            },
        )

        self.assertEqual(400, response.status_code)
        self.assertIn("两次输入的新密码不一致", response.text)

    def test_wrong_password_renders_visible_login_error(self):
        response = self.client.post(
            "/login",
            data={"username": "friend", "password": "definitely-wrong"},
            follow_redirects=False,
        )
        self.assertEqual(400, response.status_code)
        self.assertIn("用户名或密码错误", response.text)
        self.assertIn('name="password"', response.text)

    def test_login_page_offers_invite_registration(self):
        response = self.client.get("/login")

        self.assertEqual(200, response.status_code)
        self.assertIn('href="/register"', response.text)

    def test_login_stylesheet_uses_same_origin_path_behind_https_proxy(self):
        with TestClient(
            create_app(self.settings, self.engine), base_url="http://internal"
        ) as client:
            response = client.get(
                "/login",
                headers={
                    "host": "wangze.oilu.cn",
                    "x-forwarded-proto": "https",
                },
            )

        self.assertEqual(200, response.status_code)
        self.assertIn('href="/static/app.css?', response.text)
        self.assertNotIn('href="http://wangze.oilu.cn/static/app.css"', response.text)

    def test_http_mode_issues_a_session_cookie_the_browser_can_return(self):
        http_settings = replace(self.settings, secure_cookies=False)
        with TestClient(
            create_app(http_settings, self.engine), base_url="http://testserver"
        ) as client:
            response = client.post(
                "/login",
                data={"username": "friend", "password": "Temporary-123!"},
                follow_redirects=False,
            )
            self.assertNotIn("secure", response.headers["set-cookie"].lower())
            self.assertEqual(200, client.get("/change-password").status_code)

    def test_account_page_never_accepts_or_renders_manual_credentials(self):
        self.login()
        page = self.client.get("/change-password")
        marker = 'name="csrf_token" value="'
        csrf = page.text.split(marker, 1)[1].split('"', 1)[0]
        self.client.post(
            "/change-password",
            data={
                "csrf_token": csrf,
                "current_password": "Temporary-123!",
                "new_password": "Permanent-123!",
                "new_password_confirmation": "Permanent-123!",
            },
        )
        page = self.client.get("/accounts")
        self.assertNotIn('name="cookies"', page.text)
        self.assertNotIn("Cookie JSON", page.text)
        response = self.client.post(
            "/accounts",
            data={
                "csrf_token": page.text.split(marker, 1)[1].split('"', 1)[0],
                "display_name": "主账号",
                "cookies": '[{"name":"sessionid","value":"sessionid-secret"}]',
            },
            follow_redirects=False,
        )
        self.assertEqual(405, response.status_code)

    def test_task_page_loads_saved_account_conversations_without_browser_access(self):
        self.login()
        page = self.client.get("/change-password")
        marker = 'name="csrf_token" value="'
        csrf = page.text.split(marker, 1)[1].split('"', 1)[0]
        self.client.post(
            "/change-password",
            data={
                "csrf_token": csrf,
                "current_password": "Temporary-123!",
                "new_password": "Permanent-123!",
                "new_password_confirmation": "Permanent-123!",
            },
        )
        with session_scope(self.engine) as session:
            user = session.scalar(select(User).where(User.username == "friend"))
            account = AccountService(
                session,
                CookieCipher(self.settings.cookie_key_file.read_bytes()),
                AuditService(session),
            ).create(
                user.id,
                "我的抖音",
                b'[{"name":"sessionid","value":"secret-marker","url":"https://www.douyin.com"}]',
            )
            account_id = account.id
            session.add_all(
                [
                    DouyinConversation(account_id=account_id, display_name="wzlovegsy"),
                    DouyinConversation(account_id=account_id, display_name="gsy"),
                    DouyinContactIdentity(
                        account_id=account_id,
                        sec_uid="stable-user-id",
                        unique_id="wzlovegsy",
                        nickname="新的昵称",
                        remark_name="我的备注",
                    ),
                ]
            )
        tasks_page = self.client.get("/tasks")
        response = self.client.get(f"/accounts/{account_id}/conversations")

        self.assertEqual(200, response.status_code)
        self.assertEqual(
            {
                "items": [
                    {"name": "gsy", "sec_uid": None},
                    {"name": "我的备注", "sec_uid": "stable-user-id"},
                ]
            },
            response.json(),
        )
        self.assertIn('list="task-target-options"', tasks_page.text)
        self.assertIn('name="target_sec_uid"', tasks_page.text)
        self.assertIn("读取好友列表", tasks_page.text)
        self.assertNotIn("secret-marker", response.text)

    def test_new_task_binds_selected_stable_contact_identity(self):
        self.login()
        with session_scope(self.engine) as session:
            user = session.scalar(select(User).where(User.username == "friend"))
            user.must_change_password = False
            account = AccountService(
                session,
                CookieCipher(self.settings.cookie_key_file.read_bytes()),
                AuditService(session),
            ).create(
                user.id,
                "我的抖音",
                b'[{"name":"sessionid","value":"secret","url":"https://www.douyin.com"}]',
            )
            session.add(
                DouyinContactIdentity(
                    account_id=account.id,
                    sec_uid="stable-user-id",
                    nickname="新的昵称",
                )
            )
            account_id = account.id
        page = self.client.get("/tasks")
        csrf = page.text.split('name="csrf_token" value="', 1)[1].split('"', 1)[0]

        response = self.client.post(
            "/tasks",
            data={
                "csrf_token": csrf,
                "account_id": account_id,
                "target_name": "新的昵称",
                "target_sec_uid": "stable-user-id",
                "send_time": "09:00",
                "message_template": "今日火花",
            },
            follow_redirects=False,
        )

        self.assertEqual(303, response.status_code)
        with session_scope(self.engine) as session:
            task = session.scalar(select(SparkTask).where(SparkTask.owner_user_id == user.id))
            binding = session.get(SparkTaskTargetIdentity, task.id)
            self.assertEqual("stable-user-id", binding.sec_uid)

    def test_authenticated_slot_availability_is_aggregate_only(self):
        with session_scope(self.engine) as session:
            user = session.scalar(select(User).where(User.username == "friend"))
            user.must_change_password = False
            account = AccountService(
                session,
                CookieCipher(self.settings.cookie_key_file.read_bytes()),
                AuditService(session),
            ).create(
                user.id,
                "隐私账号",
                b'[{"name":"sessionid","value":"secret-slot-marker"}]',
            )
            session.add(
                SparkTask(
                    owner_user_id=user.id,
                    douyin_account_id=account.id,
                    target_name="private-target-marker",
                    send_time="11:00",
                    message_template="private-message-marker",
                    enabled=True,
                )
            )
        self.login()

        response = self.client.get("/tasks/availability?send_time=11:01")

        self.assertEqual(200, response.status_code)
        self.assertEqual(0, response.json()["remaining"])
        self.assertFalse(response.json()["available"])
        self.assertEqual(3, len(response.json()["suggestions"]))
        self.assertNotIn("private-target-marker", response.text)
        self.assertNotIn("private-message-marker", response.text)
        self.assertNotIn("secret-slot-marker", response.text)

    def test_slot_availability_requires_login(self):
        response = self.client.get(
            "/tasks/availability?send_time=11:00", follow_redirects=False
        )

        self.assertEqual(303, response.status_code)
        self.assertEqual("/login", response.headers["location"])

    def test_platform_status_api_is_authenticated_and_aggregate_only(self):
        now = datetime.now(timezone.utc)
        with session_scope(self.engine) as session:
            user = session.scalar(select(User).where(User.username == "friend"))
            user.must_change_password = False
            task = SparkTask(
                owner_user_id=user.id,
                target_name="private-dashboard-target",
                send_time="11:00",
                message_template="private-dashboard-message",
                enabled=True,
            )
            session.add(task)
            session.flush()
            session.add(
                TaskRun(
                    task_id=task.id,
                    scheduled_for=now,
                    status="success",
                    stage="complete",
                    finished_at=now,
                )
            )
            lock = session.get(WorkerLock, 1)
            lock.lease_until = now + timedelta(minutes=1)

        unauthenticated = self.client.get(
            "/api/platform-status", follow_redirects=False
        )
        self.assertEqual(303, unauthenticated.status_code)

        self.login()
        response = self.client.get("/api/platform-status")

        self.assertEqual(200, response.status_code)
        self.assertEqual(
            {
                "total",
                "success",
                "running",
                "pending",
                "failed",
                "worker_online",
                "updated_at",
            },
            set(response.json()),
        )
        self.assertEqual(1, response.json()["success"])
        self.assertTrue(response.json()["worker_online"])
        self.assertNotIn("private-dashboard-target", response.text)
        self.assertNotIn("private-dashboard-message", response.text)

    def test_dashboard_renders_live_platform_summary_hooks(self):
        now = datetime.now(timezone.utc)
        with session_scope(self.engine) as session:
            user = session.scalar(select(User).where(User.username == "friend"))
            user.must_change_password = False
            first = SparkTask(
                owner_user_id=user.id,
                target_name="首页任务一",
                send_time="15:00",
                message_template="消息",
                enabled=True,
            )
            second = SparkTask(
                owner_user_id=user.id,
                target_name="首页任务二",
                send_time="15:02",
                message_template="消息",
                enabled=True,
            )
            session.add_all((first, second))
            session.flush()
            session.add(
                TaskRun(
                    task_id=first.id,
                    scheduled_for=now,
                    status="success",
                    stage="complete",
                    finished_at=now,
                )
            )
        self.login()

        response = self.client.get("/dashboard")

        self.assertEqual(200, response.status_code)
        self.assertIn('data-platform-total="2"', response.text)
        self.assertIn('data-platform-success="1"', response.text)
        self.assertIn('data-platform-pending="1"', response.text)
        self.assertIn("今日成功", response.text)
        self.assertIn('/static/dashboard.js?', response.text)

    def test_task_page_displays_current_quota_usage(self):
        with session_scope(self.engine) as session:
            user = session.scalar(select(User).where(User.username == "friend"))
            user.must_change_password = False
            session.add(
                DouyinAccount(
                    owner_user_id=user.id,
                    display_name="额度账号",
                    encrypted_cookies=b"encrypted",
                    cookie_nonce=b"nonce",
                )
            )
            session.add(
                SparkTask(
                    owner_user_id=user.id,
                    target_name="额度任务",
                    send_time="16:00",
                    message_template="消息",
                    enabled=False,
                )
            )
        self.login()

        response = self.client.get("/tasks")

        self.assertIn("<strong>0/5</strong>", response.text)
        self.assertIn("已保存 1 个，最多保存 20 个任务", response.text)
        self.assertIn('id="task-slot-status"', response.text)
        self.assertIn('id="task-slot-suggestions"', response.text)

    def test_task_page_shows_each_quota_time_window_and_saved_task_cap(self):
        with session_scope(self.engine) as session:
            user = session.scalar(select(User).where(User.username == "friend"))
            user.must_change_password = False
        self.login()
        self.client.get("/tasks")
        with session_scope(self.engine) as session:
            grant = session.scalar(
                select(TaskQuotaGrant).where(TaskQuotaGrant.user_id == user.id)
            )
            grant.label = "限时免费额度"
            grant.expires_at = datetime(2026, 11, 1, 0, 0, tzinfo=timezone.utc)

        response = self.client.get("/tasks")

        self.assertIn("限时免费额度", response.text)
        self.assertIn("有效至", response.text)
        self.assertIn("最多保存 20 个任务", response.text)

    def test_dashboard_reconciles_expired_quota_before_reporting_platform_status(self):
        with session_scope(self.engine) as session:
            user = session.scalar(select(User).where(User.username == "friend"))
            user.must_change_password = False
            session.get(TaskQuotaPolicy, 1).default_amount = 0
            task = SparkTask(
                owner_user_id=user.id,
                target_name="额度到期任务",
                send_time="18:00",
                message_template="消息",
                enabled=True,
                next_run_at=datetime.now(timezone.utc) + timedelta(hours=1),
            )
            session.add(task)
            session.flush()
            task_id = task.id
        self.login()

        response = self.client.get("/dashboard")

        self.assertEqual(200, response.status_code)
        with session_scope(self.engine) as session:
            self.assertFalse(session.get(SparkTask, task_id).enabled)

    def test_concurrent_posts_cannot_violate_the_four_minute_gap(self):
        with session_scope(self.engine) as session:
            user = session.scalar(select(User).where(User.username == "friend"))
            user.must_change_password = False
            account = DouyinAccount(
                owner_user_id=user.id,
                display_name="并发账号",
                encrypted_cookies=b"encrypted",
                cookie_nonce=b"nonce",
            )
            session.add(account)
            session.flush()
            account_id = account.id
            user_id = user.id
        self.login()
        page = self.client.get("/tasks")
        csrf = page.text.split('name="csrf_token" value="', 1)[1].split('"', 1)[0]

        def submit(index):
            return self.client.post(
                "/tasks",
                data={
                    "csrf_token": csrf,
                    "account_id": account_id,
                    "target_name": f"并发好友{index}",
                    "target_sec_uid": "",
                    "send_time": "17:00" if index == 0 else "17:01",
                    "message_template": "今日火花",
                },
                follow_redirects=False,
            ).status_code

        with ThreadPoolExecutor(max_workers=2) as pool:
            statuses = sorted(pool.map(submit, (0, 1)))

        self.assertEqual([303, 400], statuses)
        with session_scope(self.engine) as session:
            tasks = session.scalars(
                select(SparkTask).where(SparkTask.owner_user_id == user_id)
            ).all()
            self.assertEqual(1, len(tasks))

    def test_user_can_open_and_submit_task_editor(self):
        self.login()
        with session_scope(self.engine) as session:
            user = session.scalar(select(User).where(User.username == "friend"))
            user.must_change_password = False
            account = AccountService(
                session,
                CookieCipher(self.settings.cookie_key_file.read_bytes()),
                AuditService(session),
            ).create(
                user.id,
                "我的抖音",
                b'[{"name":"sessionid","value":"secret","url":"https://www.douyin.com"}]',
            )
            session.add(
                DouyinContactIdentity(
                    account_id=account.id,
                    sec_uid="edited-stable-id",
                    nickname="编辑后的好友",
                )
            )
            task = SparkTask(
                owner_user_id=user.id,
                douyin_account_id=account.id,
                target_name="旧好友",
                send_time="09:00",
                message_template="旧消息",
                enabled=True,
            )
            session.add(task)
            session.flush()
            task_id = task.id
            account_id = account.id

        tasks_page = self.client.get("/tasks")
        self.assertIn(f'href="/tasks/{task_id}/edit"', tasks_page.text)
        edit_page = self.client.get(f"/tasks/{task_id}/edit")
        self.assertEqual(200, edit_page.status_code)
        self.assertIn("编辑续火任务", edit_page.text)
        self.assertIn('value="09:00"', edit_page.text)
        self.assertIn("旧消息", edit_page.text)
        csrf = edit_page.text.split('name="csrf_token" value="', 1)[1].split('"', 1)[0]

        response = self.client.post(
            f"/tasks/{task_id}/edit",
            data={
                "csrf_token": csrf,
                "account_id": account_id,
                "target_name": "编辑后的好友",
                "target_sec_uid": "edited-stable-id",
                "send_time": "21:30",
                "message_template": "编辑后的消息",
            },
            follow_redirects=False,
        )

        self.assertEqual(303, response.status_code)
        self.assertEqual("/tasks", response.headers["location"])
        with session_scope(self.engine) as session:
            task = session.get(SparkTask, task_id)
            binding = session.get(SparkTaskTargetIdentity, task_id)
            self.assertEqual("编辑后的好友", task.target_name)
            self.assertEqual("21:30", task.send_time)
            self.assertEqual("编辑后的消息", task.message_template)
            self.assertEqual("edited-stable-id", binding.sec_uid)

    def test_run_history_uses_chinese_status_and_shanghai_time(self):
        with session_scope(self.engine) as session:
            user = session.scalar(select(User).where(User.username == "friend"))
            user.must_change_password = False
            account = DouyinAccount(
                owner_user_id=user.id,
                display_name="我的抖音",
                encrypted_cookies=b"encrypted",
                cookie_nonce=b"nonce",
            )
            session.add(account)
            session.flush()
            task = SparkTask(
                owner_user_id=user.id,
                douyin_account_id=account.id,
                target_name="繁花",
                send_time="16:36",
                message_template="今日火花",
                enabled=True,
            )
            session.add(task)
            session.flush()
            session.add(
                TaskRun(
                    task_id=task.id,
                    scheduled_for=datetime(2026, 8, 26, 8, 36, tzinfo=timezone.utc),
                    status="success",
                    stage="submitted",
                    error_code="delivery_confirmation_unavailable",
                    error_summary="消息已提交，页面未能二次确认",
                    started_at=datetime(2026, 8, 26, 8, 36, 2, tzinfo=timezone.utc),
                    finished_at=datetime(2026, 8, 26, 8, 36, 8, tzinfo=timezone.utc),
                )
            )
            session.add(
                TaskRun(
                    task_id=task.id,
                    scheduled_for=datetime(2026, 8, 25, 8, 36, tzinfo=timezone.utc),
                    status="failed",
                    stage="worker_error",
                    error_code="unexpected_error",
                    error_summary="任务执行发生意外异常，Worker 已继续运行",
                )
            )
        self.login()

        response = self.client.get("/runs")

        self.assertEqual(200, response.status_code)
        self.assertIn("16:36", response.text)
        self.assertIn("成功", response.text)
        self.assertIn("已提交发送", response.text)
        self.assertIn("消息已提交，页面未能二次确认", response.text)
        self.assertIn('class="run-note info"', response.text)
        self.assertIn("失败", response.text)
        self.assertIn("执行异常", response.text)
        self.assertIn("繁花", response.text)
        self.assertIn('class="run-timeline"', response.text)

    def test_admin_run_history_includes_every_users_runs(self):
        password = PasswordService()
        with session_scope(self.engine) as session:
            friend = session.scalar(select(User).where(User.username == "friend"))
            friend.must_change_password = False
            admin = User(
                username="history-admin",
                password_hash=password.hash("Admin-pass-123"),
                role="admin",
                must_change_password=False,
            )
            other = User(
                username="history-other",
                password_hash=password.hash("Other-pass-123"),
                role="user",
                must_change_password=False,
            )
            session.add_all([admin, other])
            session.flush()
            for owner, target in ((friend, "好友甲"), (other, "好友乙")):
                account = DouyinAccount(
                    owner_user_id=owner.id,
                    display_name=f"{owner.username}的抖音",
                    encrypted_cookies=b"encrypted",
                    cookie_nonce=b"nonce",
                )
                session.add(account)
                session.flush()
                task = SparkTask(
                    owner_user_id=owner.id,
                    douyin_account_id=account.id,
                    target_name=target,
                    send_time="08:00",
                    message_template="测试消息",
                )
                session.add(task)
                session.flush()
                session.add(
                    TaskRun(
                        task_id=task.id,
                        scheduled_for=datetime(2026, 8, 30, 0, 0, tzinfo=timezone.utc),
                        status="success",
                        stage="complete",
                    )
                )

        self.client.post(
            "/login",
            data={"username": "history-admin", "password": "Admin-pass-123"},
            follow_redirects=False,
        )
        response = self.client.get("/runs")

        self.assertEqual(200, response.status_code)
        self.assertIn("好友甲", response.text)
        self.assertIn("好友乙", response.text)
        self.assertIn("friend · friend的抖音", response.text)
        self.assertIn("history-other · history-other的抖音", response.text)

    def test_run_history_paginates_six_entries_per_page(self):
        with session_scope(self.engine) as session:
            friend = session.scalar(select(User).where(User.username == "friend"))
            friend.must_change_password = False
            task = SparkTask(
                owner_user_id=friend.id,
                target_name="分页好友",
                send_time="08:00",
                message_template="测试消息",
            )
            session.add(task)
            session.flush()
            scheduled = datetime(2026, 8, 30, 0, 0, tzinfo=timezone.utc)
            for index in range(7):
                session.add(
                    TaskRun(
                        task_id=task.id,
                        scheduled_for=scheduled - timedelta(minutes=index),
                        status="success",
                        stage="complete",
                    )
                )
            other = User(
                username="private-history-owner",
                password_hash=PasswordService().hash("Other-pass-123"),
                role="user",
                must_change_password=False,
            )
            session.add(other)
            session.flush()
            private_task = SparkTask(
                owner_user_id=other.id,
                target_name="不应显示的好友",
                send_time="08:00",
                message_template="私有消息",
            )
            session.add(private_task)
            session.flush()
            session.add(
                TaskRun(
                    task_id=private_task.id,
                    scheduled_for=scheduled + timedelta(minutes=1),
                    status="success",
                    stage="complete",
                )
            )
        self.login()

        first_page = self.client.get("/runs")
        second_page = self.client.get("/runs?page=2")

        self.assertEqual(6, first_page.text.count('class="run-entry '))
        self.assertIn("第 1 / 2 页 · 共 7 条", first_page.text)
        self.assertIn('href="/runs?page=2#run-history"', first_page.text)
        self.assertEqual(1, second_page.text.count('class="run-entry '))
        self.assertIn("第 2 / 2 页 · 共 7 条", second_page.text)
        self.assertIn('href="/runs?page=1#run-history"', second_page.text)
        self.assertNotIn("不应显示的好友", first_page.text + second_page.text)


if __name__ == "__main__":
    unittest.main()
