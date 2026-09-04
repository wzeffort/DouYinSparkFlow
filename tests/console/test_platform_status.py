import unittest
from datetime import datetime, timedelta, timezone

from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from spark_console.db import create_schema
from spark_console.models import SparkTask, TaskRun, User, WorkerLock
from spark_console.services.platform_status import build_platform_status


class PlatformStatusTests(unittest.TestCase):
    def setUp(self):
        self.engine = create_engine("sqlite:///:memory:")
        create_schema(self.engine)
        self.session = Session(self.engine)
        self.user = User(username="status-user", password_hash="hash")
        self.session.add(self.user)
        self.session.flush()

    def tearDown(self):
        self.session.close()
        self.engine.dispose()

    def _task(self, name, enabled=True):
        task = SparkTask(
            owner_user_id=self.user.id,
            target_name=name,
            send_time="09:00",
            message_template="消息",
            enabled=enabled,
        )
        self.session.add(task)
        self.session.flush()
        return task

    def _run(self, task, when, status):
        run = TaskRun(
            task_id=task.id,
            scheduled_for=when,
            status=status,
            stage="complete" if status == "success" else "sending",
            started_at=when,
            finished_at=None if status == "running" else when + timedelta(seconds=5),
        )
        self.session.add(run)
        return run

    def test_latest_run_per_enabled_task_forms_mutually_exclusive_counts(self):
        now = datetime(2026, 8, 31, 4, 0, tzinfo=timezone.utc)
        success = self._task("成功")
        running = self._task("执行中")
        failed = self._task("失败")
        self._task("待执行")
        paused = self._task("已暂停", enabled=False)
        self._run(success, now - timedelta(minutes=20), "failed")
        self._run(success, now - timedelta(minutes=10), "success")
        self._run(running, now - timedelta(minutes=5), "running")
        self._run(failed, now - timedelta(minutes=3), "skipped")
        self._run(paused, now - timedelta(minutes=2), "success")
        self.session.flush()

        status = build_platform_status(self.session, now=now)

        self.assertEqual(4, status.total)
        self.assertEqual(1, status.success)
        self.assertEqual(1, status.running)
        self.assertEqual(1, status.pending)
        self.assertEqual(1, status.failed)

    def test_previous_shanghai_day_run_does_not_replace_today_pending(self):
        now = datetime(2026, 8, 31, 1, 0, tzinfo=timezone.utc)
        task = self._task("今日待执行")
        self._run(task, datetime(2026, 8, 30, 15, 59, tzinfo=timezone.utc), "success")
        self.session.flush()

        status = build_platform_status(self.session, now=now)

        self.assertEqual(0, status.success)
        self.assertEqual(1, status.pending)

    def test_worker_online_comes_from_current_lease(self):
        now = datetime(2026, 8, 31, 4, 0, tzinfo=timezone.utc)
        lock = self.session.get(WorkerLock, 1)
        lock.lease_until = now + timedelta(seconds=30)
        self.session.flush()
        self.assertTrue(build_platform_status(self.session, now=now).worker_online)

        lock.lease_until = now - timedelta(seconds=1)
        self.session.flush()
        self.assertFalse(build_platform_status(self.session, now=now).worker_online)


if __name__ == "__main__":
    unittest.main()
