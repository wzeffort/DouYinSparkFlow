import hashlib
import unittest
from datetime import datetime, timedelta, timezone
from unittest.mock import patch
from zoneinfo import ZoneInfo

from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from core.web_chat import DouyinUserIdentity
from spark_console.crypto import CookieCipher
from spark_console.db import create_schema
from spark_console.models import (
    AuditEvent,
    DouyinAccount,
    DouyinAccountIdentity,
    DouyinContactIdentity,
    SparkTask,
    SparkTaskTargetIdentity,
    TaskRun,
    User,
    UserTaskQuota,
)
from spark_console.security import PasswordService
from spark_console.services import NotFound, ValidationError
from spark_console.services.accounts import AccountService
from spark_console.services.audits import AuditService
from spark_console.services.tasks import TaskService, schedule_recent_safe_failures
from spark_console.services.task_capacity import TaskCapacityService
from spark_console.services.users import UserService


class ServiceTests(unittest.TestCase):
    def setUp(self):
        self.engine = create_engine("sqlite:///:memory:")
        create_schema(self.engine)
        self.session = Session(self.engine)
        self.audit = AuditService(self.session)
        self.users = UserService(self.session, PasswordService(), self.audit)
        self.accounts = AccountService(
            self.session, CookieCipher(b"c" * 32), self.audit
        )
        self.tasks = TaskService(self.session, self.accounts, self.audit)
        self.owner, _ = self.users.create("friend", "Temporary-123!", "user")
        self.other, _ = self.users.create("other", "Temporary-456!", "user")
        self.account = self.accounts.create(
            self.owner.id, "我的账号", b'[{"name":"sid","value":"secret"}]'
        )
        self.session.flush()

    def tearDown(self):
        self.session.close()
        self.engine.dispose()

    def test_user_cannot_read_or_delete_another_users_task(self):
        task = self.tasks.create(
            self.owner.id, self.account.id, "朋友", "09:00", "今日火花"
        )
        with self.assertRaises(NotFound):
            self.tasks.get_owned(self.other.id, task.id)
        with self.assertRaises(NotFound):
            self.tasks.delete_owned(self.other.id, task.id)

    def test_paused_tasks_do_not_consume_the_five_active_task_quota(self):
        created = []
        for index in range(5):
            task = self.tasks.create(
                self.owner.id,
                self.account.id,
                f"朋友{index}",
                f"09:{index * 4:02d}",
                "今日火花",
            )
            created.append(task)
        created[0].enabled = False
        self.session.flush()

        sixth = self.tasks.create(
            self.owner.id, self.account.id, "第六位朋友", "10:00", "今日火花"
        )

        capacity = TaskCapacityService(self.session, self.audit)
        self.assertIsNotNone(sixth.id)
        self.assertEqual(5, capacity.active_usage_for(self.owner.id))
        self.assertEqual(6, capacity.saved_usage_for(self.owner.id))

    def test_admin_can_raise_an_ordinary_users_task_limit(self):
        capacity = TaskCapacityService(self.session, self.audit)
        admin, _ = self.users.create("quotaadmin", "Temporary-789!", "admin")
        for index in range(5):
            self.tasks.create(
                self.owner.id,
                self.account.id,
                f"限额好友{index}",
                f"10:{index * 4:02d}",
                "今日火花",
            )

        quota = capacity.set_limit(admin.id, self.owner.id, 8)
        sixth = self.tasks.create(
            self.owner.id, self.account.id, "限额好友5", "10:20", "今日火花"
        )

        self.assertEqual(8, quota.amount)
        self.assertEqual(8, capacity.limit_for(self.owner))
        self.assertEqual(6, capacity.usage_for(self.owner.id))
        self.assertEqual(1, len(capacity.grants_for(self.owner.id)))
        self.assertIsNotNone(sixth.id)

    def test_administrator_accounts_are_not_task_limited(self):
        admin, _ = self.users.create("adminuser", "Temporary-789!", "admin")
        admin_account = self.accounts.create(
            admin.id, "管理员账号", b'[{"name":"sid","value":"admin"}]'
        )

        for index in range(7):
            self.tasks.create(
                admin.id,
                admin_account.id,
                f"管理员好友{index}",
                f"11:{index * 4:02d}",
                "今日火花",
            )

        self.assertIsNone(TaskCapacityService(self.session, self.audit).limit_for(admin))

    def test_quota_grants_stack_and_expire_at_the_exact_end_time(self):
        capacity = TaskCapacityService(self.session, self.audit)
        admin, _ = self.users.create("grantadmin", "Temporary-789!", "admin")
        starts_at = datetime.now(timezone.utc) + timedelta(seconds=1)
        expires_at = starts_at + timedelta(days=30)

        grant = capacity.grant(
            admin.id,
            self.owner.id,
            amount=5,
            starts_at=starts_at,
            expires_at=expires_at,
            label="一个月体验额度",
        )

        self.assertEqual(10, capacity.limit_for(self.owner, starts_at))
        self.assertEqual(10, capacity.limit_for(self.owner, expires_at - timedelta(microseconds=1)))
        self.assertEqual(5, capacity.limit_for(self.owner, expires_at))
        self.assertEqual("一个月体验额度", grant.label)

    def test_quota_summary_marks_active_grant_expiring_within_seven_days(self):
        capacity = TaskCapacityService(self.session, self.audit)
        admin, _ = self.users.create("warningadmin", "Temporary-789!", "admin")
        now = datetime.now(timezone.utc)
        capacity.grant(
            admin.id,
            self.owner.id,
            2,
            now - timedelta(days=1),
            now + timedelta(days=3),
            "即将到期额度",
        )

        summary = capacity.summary_for(self.owner, now)
        item = next(
            value for value in summary["grants"] if value["grant"].label == "即将到期额度"
        )

        self.assertEqual("expiring", item["status"])
        self.assertEqual(3, item["days_remaining"])

    def test_admin_policy_controls_new_user_quota_without_a_code_constant(self):
        capacity = TaskCapacityService(self.session, self.audit)
        admin, _ = self.users.create("policyadmin", "Temporary-789!", "admin")

        policy = capacity.update_policy(
            admin.id,
            default_amount=3,
            default_duration_days=45,
            max_saved_tasks=12,
        )
        newcomer, _ = self.users.create("policyfriend", "Temporary-789!", "user")
        grant = capacity.grants_for(newcomer.id)[0]

        self.assertEqual(3, policy.default_amount)
        self.assertEqual(45, policy.default_duration_days)
        self.assertEqual(12, policy.max_saved_tasks)
        self.assertEqual(3, grant.amount)
        self.assertIsNotNone(grant.expires_at)
        self.assertEqual(timedelta(days=45), grant.expires_at - grant.starts_at)

    def test_legacy_custom_limit_migrates_once_to_a_non_expiring_initial_grant(self):
        legacy = User(username="legacyquota", password_hash="hash", role="user")
        self.session.add(legacy)
        self.session.flush()
        self.session.add(UserTaskQuota(user_id=legacy.id, task_limit=7))
        self.session.flush()
        capacity = TaskCapacityService(self.session, self.audit)

        self.assertEqual(7, capacity.limit_for(legacy))
        capacity.bootstrap_user(legacy)
        grants = capacity.grants_for(legacy.id)

        self.assertEqual(1, len(grants))
        self.assertTrue(grants[0].is_initial)
        self.assertEqual(7, grants[0].amount)
        self.assertIsNone(grants[0].expires_at)

    def test_expired_quota_pauses_newest_excess_tasks_without_deleting_them(self):
        capacity = TaskCapacityService(self.session, self.audit)
        admin, _ = self.users.create("expiryadmin", "Temporary-789!", "admin")
        # Task creation evaluates grants against the real clock. Using the
        # current instant keeps this expiry scenario stable across date changes.
        now = datetime.now(timezone.utc)
        expiry = now + timedelta(days=1)
        capacity.grant(admin.id, self.owner.id, 2, now, expiry, "两席体验")
        tasks = []
        for index in range(7):
            task = self.tasks.create(
                self.owner.id,
                self.account.id,
                f"到期好友{index}",
                f"15:{index * 4:02d}",
                "今日火花",
            )
            task.created_at = now + timedelta(seconds=index)
            tasks.append(task)
        self.session.flush()

        paused_ids = capacity.reconcile_user(self.owner.id, expiry)

        self.assertEqual([tasks[5].id, tasks[6].id], paused_ids)
        self.assertEqual(7, capacity.saved_usage_for(self.owner.id))
        self.assertEqual(5, capacity.active_usage_for(self.owner.id))
        self.assertTrue(all(task.enabled for task in tasks[:5]))
        self.assertTrue(all(not task.enabled and task.next_run_at is None for task in tasks[5:]))

    def test_paused_tasks_do_not_use_active_quota_but_saved_task_cap_is_enforced(self):
        capacity = TaskCapacityService(self.session, self.audit)
        admin, _ = self.users.create("savedadmin", "Temporary-789!", "admin")
        capacity.update_policy(admin.id, 5, None, 6)
        tasks = []
        for index in range(5):
            tasks.append(
                self.tasks.create(
                    self.owner.id,
                    self.account.id,
                    f"保存好友{index}",
                    f"16:{index * 4:02d}",
                    "今日火花",
                )
            )
        self.tasks.set_enabled_owned(self.owner.id, tasks[0].id, False)
        self.tasks.create(
            self.owner.id, self.account.id, "第六个保存任务", "16:20", "今日火花"
        )

        with self.assertRaisesRegex(ValidationError, "最多保存 6 个任务"):
            self.tasks.create(
                self.owner.id, self.account.id, "第七个保存任务", "16:12", "今日火花"
            )

    def test_revoking_a_grant_immediately_pauses_excess_and_writes_an_audit_event(self):
        capacity = TaskCapacityService(self.session, self.audit)
        admin, _ = self.users.create("revokeadmin", "Temporary-789!", "admin")
        now = datetime.now(timezone.utc)
        grant = capacity.grant(
            admin.id,
            self.owner.id,
            2,
            now - timedelta(minutes=1),
            now + timedelta(days=30),
            "可撤销体验",
        )
        for index in range(7):
            self.tasks.create(
                self.owner.id,
                self.account.id,
                f"撤销好友{index}",
                f"17:{index * 4:02d}",
                "今日火花",
            )

        paused_ids = capacity.revoke(admin.id, grant.id, now)

        self.assertEqual(2, len(paused_ids))
        self.assertIsNotNone(grant.revoked_at)
        events = self.session.scalars(
            select(AuditEvent).where(
                AuditEvent.action.in_(("quota.revoked", "task.quota_auto_paused"))
            )
        ).all()
        self.assertEqual(3, len(events))

    def test_admin_can_change_every_grants_amount_and_time_window(self):
        capacity = TaskCapacityService(self.session, self.audit)
        admin, _ = self.users.create("editgrantadmin", "Temporary-789!", "admin")
        now = datetime.now(timezone.utc)
        grant = capacity.grants_for(self.owner.id)[0]

        updated = capacity.update_grant(
            admin.id,
            grant.id,
            amount=4,
            starts_at=now,
            expires_at=now + timedelta(days=60),
            label="两个月基础额度",
        )

        self.assertEqual(4, updated.amount)
        self.assertEqual("两个月基础额度", updated.label)
        self.assertEqual(timedelta(days=60), updated.expires_at - updated.starts_at)

    def test_four_minute_gap_blocks_three_neighbor_minutes_and_allows_fourth(self):
        first = self.tasks.create(
            self.owner.id, self.account.id, "十一点好友", "11:00", "今日火花"
        )
        other_account = self.accounts.create(
            self.other.id, "其他用户账号", b'[{"name":"sid","value":"other"}]'
        )

        for minute in ("11:01", "11:02", "11:03"):
            with self.subTest(minute=minute), self.assertRaisesRegex(
                ValidationError, "四分钟安全间隔"
            ):
                self.tasks.create(
                    self.other.id, other_account.id, f"相邻好友{minute}", minute, "今日火花"
                )

        second = self.tasks.create(
            self.owner.id, self.account.id, "下一时段好友", "11:04", "今日火花"
        )
        self.assertNotEqual(first.id, second.id)

    def test_pausing_releases_slot_and_enabling_rechecks_capacity(self):
        first = self.tasks.create(
            self.owner.id, self.account.id, "原时段好友", "12:00", "今日火花"
        )
        self.tasks.set_enabled_owned(self.owner.id, first.id, False)
        replacement = self.tasks.create(
            self.owner.id, self.account.id, "替代好友", "12:01", "今日火花"
        )

        with self.assertRaisesRegex(ValidationError, "四分钟安全间隔"):
            self.tasks.set_enabled_owned(self.owner.id, first.id, True)

        self.assertFalse(first.enabled)
        self.assertTrue(replacement.enabled)

    def test_editing_task_ignores_its_own_four_minute_range(self):
        task = self.tasks.create(
            self.owner.id, self.account.id, "编辑时段好友", "13:00", "今日火花"
        )

        updated = self.tasks.update_owned(
            self.owner.id,
            task.id,
            self.account.id,
            "编辑时段好友",
            "13:01",
            "今日火花",
        )

        self.assertEqual("13:01", updated.send_time)

    def test_slot_availability_returns_three_free_neighbor_times(self):
        self.tasks.create(
            self.owner.id, self.account.id, "占用时段好友", "14:00", "今日火花"
        )
        capacity = TaskCapacityService(self.session, self.audit)

        availability = capacity.availability("14:01")

        self.assertFalse(availability.available)
        self.assertEqual(0, availability.remaining)
        self.assertEqual(3, len(availability.suggestions))
        self.assertIn("13:56", availability.suggestions)
        self.assertIn("14:04", availability.suggestions)
        self.assertNotIn("14:00", availability.suggestions)

    def test_retry_time_moves_to_next_free_four_minute_gap(self):
        earliest = datetime(2026, 8, 31, 2, 1, tzinfo=timezone.utc)
        occupied = self.tasks.create(
            self.owner.id, self.account.id, "重试占位好友", "10:00", "今日火花"
        )
        occupied.next_run_at = earliest.replace(minute=0)
        self.session.flush()

        retry_at = TaskCapacityService(
            self.session, self.audit
        ).next_available_run_at(earliest)

        self.assertEqual(
            datetime(2026, 8, 31, 2, 4, tzinfo=timezone.utc), retry_at
        )

    def test_spread_enabled_schedule_only_pushes_conflicts_forward(self):
        base = datetime(2026, 9, 2, 1, 0, tzinfo=timezone.utc)
        tasks = []
        for index, send_time in enumerate(("09:00", "09:02", "09:03", "09:12")):
            task = SparkTask(
                owner_user_id=self.owner.id,
                douyin_account_id=self.account.id,
                target_name=f"迁移好友{index}",
                send_time=send_time,
                message_template="今日火花",
                enabled=True,
                next_run_at=base + timedelta(minutes=index),
            )
            self.session.add(task)
            tasks.append(task)
        self.session.flush()

        changed = TaskCapacityService(
            self.session, self.audit
        ).spread_enabled_schedule(base)

        self.assertEqual(("09:00", "09:04", "09:08", "09:12"), tuple(
            task.send_time for task in tasks
        ))
        self.assertEqual([tasks[1].id, tasks[2].id], changed)
        self.assertTrue(all(task.next_run_at > base for task in tasks[1:3]))

    def test_cookie_is_encrypted_and_never_returned_by_list(self):
        self.session.flush()
        stored = self.session.get(DouyinAccount, self.account.id)
        cookie_marker = b"secret"
        self.assertFalse(cookie_marker in stored.encrypted_cookies)
        public = self.accounts.list_owned(self.owner.id)
        self.assertEqual(1, len(public))
        self.assertEqual({"id", "display_name", "validation_state"}, set(public[0]))
        self.assertEqual(self.account.id, public[0]["id"])
        self.assertEqual("我的账号", public[0]["display_name"])
        self.assertEqual("unknown", public[0]["validation_state"])

    def test_storage_state_is_encrypted_with_identity_and_safe_projection(self):
        state = {
            "cookies": [
                {
                    "name": "sid",
                    "value": "storage-cookie-marker",
                    "domain": ".douyin.com",
                    "path": "/",
                    "expires": -1,
                    "httpOnly": True,
                    "secure": True,
                    "sameSite": "Lax",
                }
            ],
            "origins": [
                {
                    "origin": "https://www.douyin.com",
                    "localStorage": [
                        {"name": "session", "value": "local-storage-marker"}
                    ],
                }
            ],
        }

        account = self.accounts.create_from_storage_state(
            self.owner.id, " 扫码账号 ", state, " douyin-123 "
        )
        self.session.flush()

        stored = self.session.get(DouyinAccount, account.id)
        identity = self.session.get(DouyinAccountIdentity, account.id)
        encrypted = stored.encrypted_cookies
        cookie_marker = b"storage-cookie-marker"
        storage_marker = b"local-storage-marker"
        self.assertFalse(cookie_marker in encrypted)
        self.assertFalse(storage_marker in encrypted)
        self.assertEqual(2, stored.cookie_version)
        self.assertEqual("valid", stored.validation_state)
        self.assertIsNotNone(stored.last_verified_at)
        self.assertEqual("douyin-123", identity.douyin_unique_id)
        plaintext = self.accounts.cipher.decrypt(
            stored.encrypted_cookies, stored.cookie_nonce
        )
        self.assertEqual(
            "2275bd33a9235dd8a676e0a62ce3b965a24e5a28054245ec6e85c2c65717ffd1",
            hashlib.sha256(plaintext).hexdigest(),
        )
        projected = next(
            item for item in self.accounts.list_owned(self.owner.id) if item["id"] == account.id
        )
        self.assertEqual(
            {"id", "display_name", "validation_state"}, set(projected)
        )

        audits = " ".join(
            f"{event.action} {event.detail or ''}"
            for event in self.session.scalars(select(AuditEvent)).all()
        )
        self.assertFalse(cookie_marker.decode() in audits)
        self.assertFalse(storage_marker.decode() in audits)

    def test_stable_contact_identity_is_saved_and_bound_to_new_task(self):
        state = {
            "cookies": [
                {
                    "name": "sid",
                    "value": "secret",
                    "domain": ".douyin.com",
                    "path": "/",
                    "expires": -1,
                    "httpOnly": True,
                    "secure": True,
                    "sameSite": "Lax",
                }
            ],
            "origins": [],
        }
        account = self.accounts.create_from_storage_state(
            self.owner.id,
            "稳定账号",
            state,
            contact_identities=(
                DouyinUserIdentity(
                    sec_uid="stable-user-id",
                    short_id="123456",
                    unique_id="search-id",
                    nickname="新的昵称",
                    remark_name="我的备注",
                ),
            ),
        )

        task = self.tasks.create(
            self.owner.id,
            account.id,
            "我的备注",
            "09:00",
            "今日火花",
            target_sec_uid="stable-user-id",
        )
        self.session.flush()

        contact = self.session.get(
            DouyinContactIdentity, (account.id, "stable-user-id")
        )
        binding = self.session.get(SparkTaskTargetIdentity, task.id)
        self.assertEqual("新的昵称", contact.nickname)
        self.assertEqual("我的备注", contact.remark_name)
        self.assertEqual("stable-user-id", binding.sec_uid)

    def test_relogin_reuses_same_account_and_keeps_existing_tasks_attached(self):
        old_state = {
            "cookies": [{"name": "sid", "value": "old", "domain": ".douyin.com", "path": "/", "expires": -1, "httpOnly": True, "secure": True, "sameSite": "Lax"}],
            "origins": [],
        }
        account = self.accounts.create_from_storage_state(
            self.owner.id,
            "旧昵称",
            old_state,
            "stable-douyin-id",
            conversation_names=("旧会话",),
            contact_identities=(
                DouyinUserIdentity(sec_uid="old-contact", nickname="旧好友"),
            ),
        )
        task = self.tasks.create(
            self.owner.id, account.id, "旧好友", "09:00", "今日火花"
        )
        account.validation_state = "invalid"
        self.session.flush()

        rebound = self.accounts.create_from_storage_state(
            self.owner.id,
            "新昵称",
            {
                "cookies": [{"name": "sid", "value": "new", "domain": ".douyin.com", "path": "/", "expires": -1, "httpOnly": True, "secure": True, "sameSite": "Lax"}],
                "origins": [],
            },
            "stable-douyin-id",
            conversation_names=("新会话",),
            contact_identities=(
                DouyinUserIdentity(sec_uid="new-contact", nickname="新好友"),
            ),
        )
        self.session.flush()

        self.assertEqual(account.id, rebound.id)
        self.assertEqual("新昵称", rebound.display_name)
        self.assertEqual("valid", rebound.validation_state)
        self.assertEqual(account.id, self.session.get(SparkTask, task.id).douyin_account_id)
        self.assertEqual(
            2,
            len(
                self.session.scalars(
                    select(DouyinAccount).where(
                        DouyinAccount.owner_user_id == self.owner.id
                    )
                ).all()
            ),
        )
        self.assertIsNone(
            self.session.get(DouyinContactIdentity, (account.id, "old-contact"))
        )
        self.assertIsNotNone(
            self.session.get(DouyinContactIdentity, (account.id, "new-contact"))
        )

    def test_relogin_schedules_each_recent_safe_failure_once(self):
        now = datetime(2026, 8, 31, 2, 0, tzinfo=timezone.utc)
        task = self.tasks.create(
            self.owner.id, self.account.id, "朋友", "09:00", "今日火花"
        )
        older = TaskRun(
            task_id=task.id,
            scheduled_for=now - timedelta(minutes=40),
            status="failed",
            stage="selecting_target",
            finished_at=now - timedelta(minutes=39),
            error_code="target_not_found",
            error_summary="未找到好友",
        )
        latest = TaskRun(
            task_id=task.id,
            scheduled_for=now - timedelta(minutes=10),
            status="failed",
            stage="authenticating",
            finished_at=now - timedelta(minutes=9),
            error_code="login_expired",
            error_summary="账号信息已过期",
        )
        self.session.add_all((older, latest))
        self.session.flush()

        scheduled = schedule_recent_safe_failures(
            self.session, self.account.id, now=now
        )
        scheduled_again = schedule_recent_safe_failures(
            self.session, self.account.id, now=now
        )

        self.assertEqual([task.id], scheduled)
        self.assertEqual([], scheduled_again)
        self.assertEqual(now + timedelta(minutes=1), task.next_run_at)
        self.assertEqual("未找到好友", older.error_summary)
        self.assertIn("重新登录成功，已安排自动补跑", latest.error_summary)

    def test_relogin_does_not_retry_unsafe_old_disabled_or_later_success(self):
        now = datetime(2026, 8, 31, 2, 0, tzinfo=timezone.utc)
        task_index = 0

        def task_with_run(name, *, stage, age_minutes, enabled=True, later_success=False):
            nonlocal task_index
            task = self.tasks.create(
                self.owner.id,
                self.account.id,
                name,
                f"09:{task_index * 4:02d}",
                "今日火花",
            )
            task_index += 1
            task.enabled = enabled
            failed_at = now - timedelta(minutes=age_minutes)
            self.session.add(
                TaskRun(
                    task_id=task.id,
                    scheduled_for=failed_at,
                    status="failed",
                    stage=stage,
                    finished_at=failed_at,
                    error_code="test_failure",
                    error_summary="测试失败",
                )
            )
            if later_success:
                self.session.add(
                    TaskRun(
                        task_id=task.id,
                        scheduled_for=failed_at + timedelta(minutes=1),
                        status="success",
                        stage="complete",
                        finished_at=failed_at + timedelta(minutes=2),
                    )
                )
            return task

        unsafe = task_with_run("已发送阶段", stage="sending", age_minutes=10)
        old = task_with_run("过期失败", stage="authenticating", age_minutes=121)
        disabled = task_with_run(
            "已暂停任务", stage="selecting_target", age_minutes=10, enabled=False
        )
        succeeded = task_with_run(
            "后来成功", stage="authenticating", age_minutes=10, later_success=True
        )
        original = {
            item.id: item.next_run_at for item in (unsafe, old, disabled, succeeded)
        }
        self.session.flush()

        scheduled = schedule_recent_safe_failures(
            self.session, self.account.id, now=now
        )

        self.assertEqual([], scheduled)
        for item in (unsafe, old, disabled, succeeded):
            self.assertEqual(original[item.id], item.next_run_at)

    def test_task_rejects_contact_identity_owned_by_another_account(self):
        other_account = self.accounts.create(
            self.owner.id, "其他账号", b'[{"name":"sid","value":"other"}]'
        )
        self.session.add(
            DouyinContactIdentity(
                account_id=other_account.id,
                sec_uid="other-account-contact",
                nickname="不属于当前账号",
            )
        )
        self.session.flush()

        with self.assertRaises(ValidationError):
            self.tasks.create(
                self.owner.id,
                self.account.id,
                "不属于当前账号",
                "09:00",
                "今日火花",
                target_sec_uid="other-account-contact",
            )

    def test_owner_can_update_task_schedule_message_and_stable_target(self):
        self.session.add(
            DouyinContactIdentity(
                account_id=self.account.id,
                sec_uid="updated-stable-id",
                nickname="更新后的好友",
            )
        )
        self.session.flush()
        task = self.tasks.create(
            self.owner.id, self.account.id, "旧好友", "09:00", "旧消息"
        )
        task.enabled = False

        updated = self.tasks.update_owned(
            self.owner.id,
            task.id,
            self.account.id,
            "更新后的好友",
            "21:30",
            "更新后的消息",
            target_sec_uid="updated-stable-id",
        )
        self.session.flush()

        binding = self.session.get(SparkTaskTargetIdentity, task.id)
        self.assertEqual("更新后的好友", updated.target_name)
        self.assertEqual("21:30", updated.send_time)
        self.assertEqual("更新后的消息", updated.message_template)
        self.assertFalse(updated.enabled)
        self.assertEqual("updated-stable-id", binding.sec_uid)
        local_next = updated.next_run_at.replace(tzinfo=timezone.utc).astimezone(
            ZoneInfo("Asia/Shanghai")
        )
        self.assertEqual((21, 30), (local_next.hour, local_next.minute))

    def test_updating_to_manual_target_removes_stale_identity_binding(self):
        self.session.add(
            DouyinContactIdentity(
                account_id=self.account.id,
                sec_uid="old-stable-id",
                nickname="旧好友",
            )
        )
        self.session.flush()
        task = self.tasks.create(
            self.owner.id,
            self.account.id,
            "旧好友",
            "09:00",
            "旧消息",
            target_sec_uid="old-stable-id",
        )

        self.tasks.update_owned(
            self.owner.id,
            task.id,
            self.account.id,
            "手动输入好友",
            "10:15",
            "新消息",
        )
        self.session.flush()

        self.assertIsNone(self.session.get(SparkTaskTargetIdentity, task.id))

    def test_storage_state_rejects_empty_cookies_before_creating_account(self):
        before = len(self.session.scalars(select(DouyinAccount)).all())

        with self.assertRaises(ValidationError):
            self.accounts.create_from_storage_state(
                self.owner.id,
                "扫码账号",
                {"cookies": [], "origins": []},
            )

        self.assertEqual(before, len(self.session.scalars(select(DouyinAccount)).all()))

    def test_storage_state_rejects_playwright_invalid_domain_before_encryption(self):
        before = len(self.session.scalars(select(DouyinAccount)).all())
        invalid_domains = (
            "\u0301a.com",
            "foo%3Abar",
            "@",
            "#",
            "?",
            "999.999.999",
            "256.1.1.1.",
        )

        with patch.object(
            self.accounts.cipher,
            "encrypt",
            side_effect=AssertionError("encryption boundary crossed"),
        ):
            for case, domain in enumerate(invalid_domains):
                with self.subTest(case=case):
                    state = {
                        "cookies": [
                            {
                                "name": "probe",
                                "value": "x",
                                "domain": domain,
                                "path": "/",
                                "expires": -1,
                                "httpOnly": True,
                                "secure": True,
                                "sameSite": "Lax",
                            }
                        ],
                        "origins": [],
                    }
                    with self.assertRaises(ValidationError):
                        self.accounts.create_from_storage_state(
                            self.owner.id, "扫码账号", state
                        )

        after = len(self.session.scalars(select(DouyinAccount)).all())
        self.assertEqual(before, after)

    def test_owner_can_rename_account_but_another_user_cannot(self):
        renamed = self.accounts.rename_owned(
            self.owner.id, self.account.id, " 生活号 "
        )

        self.assertEqual("生活号", renamed.display_name)
        rename_events = self.session.scalars(
            select(AuditEvent).where(AuditEvent.action == "account.renamed")
        ).all()
        self.assertEqual(1, len(rename_events))
        self.assertTrue(all(event.detail is None for event in rename_events))
        with self.assertRaises(NotFound):
            self.accounts.rename_owned(self.other.id, self.account.id, "越权名称")
        with self.assertRaises(ValidationError):
            self.accounts.rename_owned(self.owner.id, self.account.id, " ")

    def test_deleting_account_erases_cookie_and_disables_tasks(self):
        task = self.tasks.create(
            self.owner.id, self.account.id, "朋友", "09:00", "今日火花"
        )
        self.session.flush()
        self.accounts.delete_owned(self.owner.id, self.account.id)
        self.session.flush()
        self.assertIsNone(self.session.get(DouyinAccount, self.account.id))
        retained = self.session.get(SparkTask, task.id)
        self.assertFalse(retained.enabled)
        self.assertIsNone(retained.douyin_account_id)

    def test_validation_rejects_bad_time_and_empty_message(self):
        with self.assertRaises(ValidationError):
            self.tasks.create(self.owner.id, self.account.id, "朋友", "25:00", "x")
        with self.assertRaises(ValidationError):
            self.tasks.create(self.owner.id, self.account.id, "朋友", "09:00", " ")

    def test_audit_records_never_contain_cookie_payload(self):
        events = self.session.scalars(select(AuditEvent)).all()
        self.assertTrue(events)
        serialized = " ".join((e.detail or "") + e.action for e in events)
        cookie_marker = "secret"
        self.assertFalse(cookie_marker in serialized)


if __name__ == "__main__":
    unittest.main()
