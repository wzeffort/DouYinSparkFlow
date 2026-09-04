from __future__ import annotations

import asyncio
import os
import signal
import socket
import uuid
from datetime import datetime, timedelta, timezone

from sqlalchemy import select, update

from spark_console.config import Settings
from spark_console.crypto import CookieCipher
from spark_console.pii import PiiCipher
from spark_console.db import create_engine_for, create_schema, session_scope
from spark_console.executor import DouyinExecutor
from spark_console.models import (
    DouyinAccount,
    NotificationPreference,
    SparkTask,
    SparkTaskTargetIdentity,
    TaskRun,
    User,
    WorkerLock,
)
from spark_console.scheduler import claim_next_due_task, finish_run
from spark_console.services.accounts import AccountService
from spark_console.services.audits import AuditService
from spark_console.services.task_capacity import TaskCapacityService
from spark_console.services.notifications import NotificationService


class Worker:
    STARTUP_GRACE = timedelta(minutes=5)
    RETRY_DELAYS = (timedelta(minutes=1), timedelta(minutes=5))
    EXECUTION_TIMEOUT_SECONDS = 180

    def __init__(
        self,
        settings: Settings,
        engine,
        executor=None,
        clock_offset_seconds=0.0,
        started_at: datetime | None = None,
        execution_timeout_seconds: float | None = None,
    ):
        self.settings = settings
        self.engine = engine
        self.executor = executor or DouyinExecutor()
        self.worker_id = f"{socket.gethostname()}-{os.getpid()}"
        self.clock_offset_seconds = clock_offset_seconds
        self.started_at = started_at or datetime.now(timezone.utc)
        self.execution_timeout_seconds = (
            execution_timeout_seconds or self.EXECUTION_TIMEOUT_SECONDS
        )
        self.cipher = CookieCipher(settings.cookie_key_file.read_bytes())
        self.pii = (
            PiiCipher(settings.pii_key_file.read_bytes())
            if settings.email_enabled and settings.pii_key_file is not None
            else None
        )
        self._recover_interrupted_runs()

    def _recover_interrupted_runs(self) -> None:
        with session_scope(self.engine) as db:
            interrupted = db.scalars(
                select(TaskRun).where(
                    TaskRun.status == "running",
                    TaskRun.finished_at.is_(None),
                )
            ).all()
            for run in interrupted:
                finish_run(
                    run,
                    "failed",
                    "worker_restart",
                    self.started_at,
                    "worker_interrupted",
                    "执行器重启中断了任务，已安排 1 分钟后重试",
                )
                task = db.get(SparkTask, run.task_id)
                if task is not None and task.enabled:
                    task.next_run_at = TaskCapacityService(
                        db, AuditService(db)
                    ).next_available_run_at(
                        self.started_at + self.RETRY_DELAYS[0], task.id
                    )

    async def run_once(self, now: datetime | None = None):
        current_time = now or datetime.now(timezone.utc)
        with session_scope(self.engine) as db:
            lock = db.get(WorkerLock, 1)
            if lock is None:
                lock = WorkerLock(id=1)
                db.add(lock)
            lock.worker_id = self.worker_id
            lock.lease_until = current_time + timedelta(
                seconds=max(30, self.settings.worker_poll_seconds * 3)
            )
            TaskCapacityService(db, AuditService(db)).reconcile_all(current_time)
            run = claim_next_due_task(db, current_time, self.worker_id)
            if run is None:
                return None
            lock.lease_until = current_time + timedelta(
                seconds=max(
                    30,
                    int(self.execution_timeout_seconds)
                    + self.settings.worker_poll_seconds * 3,
                )
            )
            task = db.get(SparkTask, run.task_id)
            if abs(self.clock_offset_seconds) > self.settings.clock_offset_limit_seconds:
                return finish_run(run, "failed", "clock_check", current_time, "system_time_unhealthy", "服务器时间未同步")
            scheduled = run.scheduled_for if run.scheduled_for.tzinfo else run.scheduled_for.replace(tzinfo=timezone.utc)
            if scheduled < self.started_at and current_time - scheduled > self.STARTUP_GRACE:
                return finish_run(
                    run,
                    "skipped",
                    "missed_startup",
                    current_time,
                    "worker_was_offline",
                    "执行器离线期间任务已错过，未补发",
                )
            if current_time - scheduled > timedelta(minutes=10):
                return finish_run(run, "skipped", "late", current_time, "missed_window", "任务已超过 10 分钟发送窗口")
            account_service = AccountService(db, self.cipher, AuditService(db))
            account = db.get(DouyinAccount, task.douyin_account_id)
            credential_version = account.cookie_version
            target_identity = db.get(SparkTaskTargetIdentity, task.id)
            target_sec_uid = target_identity.sec_uid if target_identity else None
            cookies = account_service.decrypt_for_worker(task.douyin_account_id)
            run_id = run.id
            task_id = task.id
            account_id = account.id
            target_name = task.target_name
            message_template = task.message_template

        try:
            timed_out = False
            try:
                result = await asyncio.wait_for(
                    self.executor.execute(
                        cookies,
                        target_name,
                        message_template,
                        credential_version=credential_version,
                        target_sec_uid=target_sec_uid,
                    ),
                    timeout=self.execution_timeout_seconds,
                )
            except TimeoutError:
                timed_out = True
                result = None
            except Exception:
                result = None
        finally:
            cookies[:] = b"\0" * len(cookies)
            cookies.clear()

        with session_scope(self.engine) as db:
            run = db.get(TaskRun, run_id)
            task = db.get(SparkTask, task_id)
            if timed_out:
                finished = finish_run(
                    run,
                    "failed",
                    "worker_timeout",
                    datetime.now(timezone.utc),
                    "execution_timeout",
                    "页面操作超过 3 分钟，已终止本次执行",
                )
                self._record_task_failure_incident(db, task, finished, datetime.now(timezone.utc))
                return finished
            if result is None:
                finished = finish_run(
                    run,
                    "failed",
                    "worker_error",
                    datetime.now(timezone.utc),
                    "unexpected_error",
                    "任务执行发生意外异常，Worker 已继续运行",
                )
                self._record_task_failure_incident(db, task, finished, datetime.now(timezone.utc))
                return finished
            if result.success:
                db.execute(
                    update(DouyinAccount)
                    .where(DouyinAccount.id == account_id)
                    .values(
                        validation_state="valid",
                        last_verified_at=datetime.now(timezone.utc),
                        invalidated_at=None,
                        invalid_reason_code=None,
                        auth_incident_id=None,
                    )
                )
            elif result.error_code in {"cookie_invalid", "login_expired"}:
                self._record_auth_incident(
                    db,
                    db.get(DouyinAccount, account_id),
                    result.error_code,
                    datetime.now(timezone.utc),
                )
            if not result.success and result.retryable:
                retry = self._schedule_retry(
                    db, task, run, current_time, result.stage
                )
                if retry is not None:
                    return retry
            finished = finish_run(
                run,
                "success" if result.success else "failed",
                result.stage,
                datetime.now(timezone.utc),
                result.error_code,
                result.error_summary,
            )
            if not result.success:
                self._record_task_failure_incident(
                    db, task, finished, datetime.now(timezone.utc)
                )
            return finished

    def _record_auth_incident(
        self, db, account: DouyinAccount, reason_code: str, now: datetime
    ) -> None:
        account.validation_state = "invalid"
        account.invalidated_at = account.invalidated_at or now
        account.invalid_reason_code = reason_code
        db.execute(
            update(SparkTask)
            .where(
                SparkTask.douyin_account_id == account.id,
                SparkTask.enabled.is_(True),
            )
            .values(enabled=False, next_run_at=None)
        )
        if account.auth_incident_id is not None:
            return
        incident_id = str(uuid.uuid4())
        account.auth_incident_id = incident_id
        if self.pii is None:
            return
        service = NotificationService(db, self.pii, AuditService(db))
        user = db.get(User, account.owner_user_id)
        preference = db.get(NotificationPreference, account.owner_user_id)
        wants_email = preference is None or preference.douyin_login_expired_email
        if (
            user is None
            or not wants_email
            or not user.email_verified_at
            or not user.email_ciphertext
            or not user.email_nonce
        ):
            return
        email = self.pii.decrypt_email(
            user.email_ciphertext,
            user.email_nonce,
            aad=f"user:{user.id}".encode(),
        )
        token = service.create_action_token(user.id, incident_id, now)
        service.enqueue_template(
            user.id,
            "douyin_login_expired",
            email,
            "douyin_expired",
            {
                "account_name": account.display_name,
                "action_path": f"/email-actions/{token}",
            },
            f"douyin-auth:{incident_id}:email",
            now=now,
        )

    def _record_task_failure_incident(
        self,
        db,
        task: SparkTask,
        run: TaskRun,
        now: datetime,
    ) -> None:
        if self.pii is None or run.status != "failed":
            return
        recent = db.scalars(
            select(TaskRun)
            .where(TaskRun.task_id == task.id)
            .order_by(TaskRun.scheduled_for.desc(), TaskRun.id.desc())
        ).all()
        streak = []
        for candidate in recent:
            if candidate.status != "failed":
                break
            streak.append(candidate)
        if not streak:
            return
        first_failure = streak[-1]
        if run.error_code == "target_not_found":
            kind = "task_target_not_found"
            reason = "请检查好友昵称或备注是否已经修改，并在任务中重新选择好友。"
        elif len(streak) == 3 and run.error_code not in {
            "login_expired",
            "cookie_invalid",
        }:
            kind = "task_repeated_failure"
            reason = "任务已连续执行失败 3 次，请查看执行记录并检查任务设置。"
        else:
            return
        incident_key = f"task-failure:{task.id}:{first_failure.id}:{kind}"
        service = NotificationService(db, self.pii, AuditService(db))
        action_path = f"/tasks/{task.id}/edit"
        user = db.get(User, task.owner_user_id)
        preference = db.get(NotificationPreference, task.owner_user_id)
        if (
            user is None
            or preference is None
            or not preference.task_repeated_failure_email
            or not user.email_verified_at
            or not user.email_ciphertext
            or not user.email_nonce
        ):
            return
        email = self.pii.decrypt_email(
            user.email_ciphertext,
            user.email_nonce,
            aad=f"user:{user.id}".encode(),
        )
        service.enqueue_template(
            user.id,
            kind,
            email,
            "task_failure",
            {
                "target_name": task.target_name,
                "reason": reason,
                "action_path": action_path,
            },
            f"{incident_key}:email",
            now=now,
        )

    def _schedule_retry(self, db, task, run, now, stage):
        retry_codes = tuple(
            f"retry_scheduled_{int(delay.total_seconds() // 60)}m"
            for delay in self.RETRY_DELAYS
        )
        used_codes = set(
            db.scalars(
                select(TaskRun.error_code).where(
                    TaskRun.task_id == task.id,
                    TaskRun.started_at >= now - timedelta(minutes=15),
                    TaskRun.error_code.in_(retry_codes),
                )
            ).all()
        )
        for delay, code in zip(self.RETRY_DELAYS, retry_codes):
            if code in used_codes:
                continue
            minutes = int(delay.total_seconds() // 60)
            task.next_run_at = TaskCapacityService(
                db, AuditService(db)
            ).next_available_run_at(now + delay, task.id)
            return finish_run(
                run,
                "failed",
                stage,
                now,
                code,
                f"发送前遇到临时故障，已安排不少于 {minutes} 分钟后的空闲时段重试",
            )
        return None


async def run_loop() -> None:
    settings = Settings.from_env(os.environ)
    engine = create_engine_for(settings)
    create_schema(engine)
    worker = Worker(settings, engine, clock_offset_seconds=float(os.environ.get("SPARK_CLOCK_OFFSET_SECONDS", "0")))
    stopping = asyncio.Event()
    loop = asyncio.get_running_loop()
    for signum in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(signum, stopping.set)
        except NotImplementedError:
            pass
    while not stopping.is_set():
        await worker.run_once()
        try:
            await asyncio.wait_for(stopping.wait(), timeout=settings.worker_poll_seconds)
        except TimeoutError:
            continue


if __name__ == "__main__":
    asyncio.run(run_loop())
