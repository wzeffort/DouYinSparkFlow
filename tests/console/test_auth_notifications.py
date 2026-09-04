import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from spark_console.config import Settings
from spark_console.db import create_engine_for, create_schema, session_scope
from spark_console.models import (
    DouyinAccount,
    NotificationEvent,
    NotificationPreference,
    SparkTask,
    TaskRun,
    User,
    UserNotification,
)
from spark_console.pii import PiiCipher
from spark_console.services.audits import AuditService
from spark_console.services.notifications import NotificationService
from spark_console.worker import Worker


class AuthenticationNotificationTests(unittest.TestCase):
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

    def tearDown(self):
        self.engine.dispose()
        self.temp.cleanup()

    def test_expired_login_pauses_tasks_and_emails_only_once_per_incident(self):
        pii = PiiCipher(b"p" * 32)
        now = datetime(2026, 9, 1, tzinfo=timezone.utc)
        with session_scope(self.engine) as db:
            user = User(username="alice", password_hash="hash")
            db.add(user)
            db.flush()
            ciphertext, nonce = pii.encrypt_email(
                "alice@example.com", aad=f"user:{user.id}".encode()
            )
            user.email_ciphertext = ciphertext
            user.email_nonce = nonce
            user.email_lookup_hash = pii.lookup_hash("alice@example.com")
            user.email_verified_at = now
            account = DouyinAccount(
                owner_user_id=user.id,
                display_name="主账号",
                encrypted_cookies=b"cipher",
                cookie_nonce=b"n" * 12,
            )
            db.add(account)
            db.flush()
            task = SparkTask(
                owner_user_id=user.id,
                douyin_account_id=account.id,
                target_name="好友",
                send_time="09:00",
                message_template="今日火花",
                enabled=True,
            )
            db.add(task)
            db.flush()
            account_id, task_id = account.id, task.id

        worker = Worker(self.settings, self.engine, executor=object())
        with session_scope(self.engine) as db:
            account = db.get(DouyinAccount, account_id)
            worker._record_auth_incident(db, account, "login_expired", now)
            first_incident = account.auth_incident_id
            worker._record_auth_incident(db, account, "login_expired", now)
            self.assertFalse(db.get(SparkTask, task_id).enabled)
            self.assertEqual(first_incident, account.auth_incident_id)
            self.assertEqual(0, db.query(UserNotification).count())
            self.assertEqual(1, db.query(NotificationEvent).count())

    def _verified_task(self, db, now):
        pii = PiiCipher(b"p" * 32)
        user = User(username="task-owner", password_hash="hash")
        db.add(user)
        db.flush()
        ciphertext, nonce = pii.encrypt_email(
            "task-owner@example.com", aad=f"user:{user.id}".encode()
        )
        user.email_ciphertext = ciphertext
        user.email_nonce = nonce
        user.email_lookup_hash = pii.lookup_hash("task-owner@example.com")
        user.email_verified_at = now
        db.add(
            NotificationPreference(
                user_id=user.id,
                task_repeated_failure_email=True,
            )
        )
        account = DouyinAccount(
            owner_user_id=user.id,
            display_name="主账号",
            encrypted_cookies=b"cipher",
            cookie_nonce=b"n" * 12,
        )
        db.add(account)
        db.flush()
        task = SparkTask(
            owner_user_id=user.id,
            douyin_account_id=account.id,
            target_name="旧备注",
            send_time="09:00",
            message_template="今日火花",
            enabled=True,
        )
        db.add(task)
        db.flush()
        return task

    def test_target_not_found_notifies_once_and_mentions_changed_remark(self):
        now = datetime(2026, 9, 2, tzinfo=timezone.utc)
        worker = Worker(self.settings, self.engine, executor=object())
        with session_scope(self.engine) as db:
            task = self._verified_task(db, now)
            run = TaskRun(
                task_id=task.id,
                scheduled_for=now,
                status="failed",
                stage="selecting_target",
                error_code="target_not_found",
                error_summary="未找到完全匹配的目标好友",
                finished_at=now,
            )
            db.add(run)
            db.flush()

            worker._record_task_failure_incident(db, task, run, now)
            worker._record_task_failure_incident(db, task, run, now)

            event = db.query(NotificationEvent).one()
            self.assertEqual(0, db.query(UserNotification).count())
            self.assertEqual("task_target_not_found", event.kind)
            self.assertEqual("task_failure", event.template_key)
            self.assertIn("备注", NotificationService(
                db, PiiCipher(b"p" * 32), AuditService(db)
            ).payload_for(event)["reason"])

    def test_third_consecutive_failure_notifies_but_fourth_does_not_duplicate(self):
        now = datetime(2026, 9, 2, tzinfo=timezone.utc)
        worker = Worker(self.settings, self.engine, executor=object())
        with session_scope(self.engine) as db:
            task = self._verified_task(db, now)
            for index in range(4):
                run = TaskRun(
                    task_id=task.id,
                    scheduled_for=now + timedelta(minutes=index),
                    status="failed",
                    stage="selecting_target",
                    error_code="conversation_not_opened",
                    error_summary="聊天窗口没有打开",
                    finished_at=now + timedelta(minutes=index),
                )
                db.add(run)
                db.flush()
                worker._record_task_failure_incident(
                    db, task, run, now + timedelta(minutes=index)
                )
                expected = 1 if index >= 2 else 0
                self.assertEqual(0, db.query(UserNotification).count())
                self.assertEqual(expected, db.query(NotificationEvent).count())
            self.assertEqual(
                "task_repeated_failure", db.query(NotificationEvent).one().kind
            )


if __name__ == "__main__":
    unittest.main()
