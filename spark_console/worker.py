from __future__ import annotations

import asyncio
import os
import signal
import socket
import uuid
from datetime import datetime, timedelta, timezone

from sqlalchemy import or_, select, update

from spark_console.config import Settings
from spark_console.crypto import CookieCipher
from spark_console.pii import PiiCipher
from spark_console.db import create_engine_for, create_schema, session_scope
from spark_console.executor import DouyinExecutor
from spark_console.execution_diagnostics import trace_run
from spark_console.models import (
    DouyinAccount,
    NotificationPreference,
    SparkTask,
    SparkTaskTargetIdentity,
    TaskRun,
    User,
    WorkerLock,
    SparkTaskRecipient,
)
from spark_console.scheduler import claim_next_due_task, finish_run
from spark_console.services.accounts import AccountService
from spark_console.services.audits import AuditService
from spark_console.services.task_capacity import TaskCapacityService
from spark_console.services.notifications import NotificationService
from spark_console.services.batch_execution import BatchRunService


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
        recovery_at: datetime | None = None,
    ):
        self.settings = settings
        self.engine = engine
        self.executor = executor or DouyinExecutor()
        self.worker_id = f"{socket.gethostname()}-{os.getpid()}"
        self.clock_offset_seconds = clock_offset_seconds
        self.started_at = started_at or datetime.now(timezone.utc)
        self.recovery_at = recovery_at or self.started_at
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
                batch = BatchRunService(db)
                if batch.rows(run.id):
                    batch.interrupt(run.id, self.recovery_at)
                    status, summary, retryable = batch.summary(run.id)
                    finish_run(run, status, "batch_interrupted", self.recovery_at,
                               "batch_interrupted" if status != "success" else None, summary)
                    task = db.get(SparkTask, run.task_id)
                    if retryable and task and task.enabled:
                        task.next_run_at = TaskCapacityService(db, AuditService(db)).next_available_run_at(
                            self.recovery_at + self.RETRY_DELAYS[0], task.id)
                        batch.remember_retry(task.id, run.id, task.next_run_at)
                    continue
                finish_run(
                    run,
                    "failed",
                    "worker_restart",
                    self.recovery_at,
                    "delivery_uncertain",
                    "执行器中断，旧任务缺少逐好友发送检查点；请核实结果，不会自动重发",
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
            # Reconcile every task of due owners, including quota migration, but
            # keep unrelated users out of the serialized scheduling transaction.
            TaskCapacityService(db, AuditService(db)).reconcile_all(current_time, due_only=True)
            run = claim_next_due_task(db, current_time, self.worker_id)
            if run is None:
                return None
            # Claim only within the reconciled horizon; use fresh time for the
            # lease/window after admission so another owner cannot bypass quotas.
            if now is None:
                current_time = datetime.now(timezone.utc)
                run.started_at = current_time
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
            is_batch = db.scalar(select(SparkTaskRecipient.task_id).where(
                SparkTaskRecipient.task_id == task.id).limit(1)) is not None
            pending_recipients = BatchRunService(db).prepare(run, task) if is_batch else []
            target_sec_uid = target_identity.sec_uid if target_identity else None
            cookies = account_service.decrypt_for_worker(task.douyin_account_id)
            run_id = run.id
            task_id = task.id
            account_id = account.id
            target_name = task.target_name
            message_template = task.message_template

        if is_batch:
            try:
                return await self._run_batch(run_id, task_id, account_id, cookies,
                                             credential_version, pending_recipients, current_time)
            finally:
                cookies[:] = b"\0" * len(cookies)
                cookies.clear()

        try:
            timed_out = False
            try:
                with trace_run(self.engine, run_id):
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
                    db, task, run, datetime.now(timezone.utc) if now is None else current_time, result.stage
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

    def _enrich_recipient_sec_uids(self, recipients, task_id, account_id, *, run_id=None):
        # Recipients created before sec_uid capture rely on nickname search,
        # which times out for emoji and special-character names. The contact
        # identity store already knows each friend's sec_uid; resolve and
        # persist it once so the executor always navigates directly. Search
        # stays as the fallback when no unique identity exists.
        pending = [r for r in recipients if not r.get("target_sec_uid")]
        if not pending:
            return
        from spark_console.models import DouyinContactIdentity, SparkTaskRecipient, TaskRunRecipient
        from spark_console.services.recipient_review import name_approvals
        resolved = []
        try:
            with session_scope(self.engine) as db:
                for recipient in pending:
                    name = (recipient.get("target_name") or "").strip()
                    if not name:
                        continue
                    identities = db.scalars(
                        select(DouyinContactIdentity).where(
                            DouyinContactIdentity.account_id == account_id,
                            or_(DouyinContactIdentity.nickname == name,
                                DouyinContactIdentity.remark_name == name),
                        )
                    ).all()
                    sec_uids = {i.sec_uid for i in identities if i.sec_uid}
                    if len(sec_uids) != 1:
                        continue
                    sec_uid = sec_uids.pop()
                    row = db.get(SparkTaskRecipient, (task_id, recipient["position"]))
                    if row is None or row.target_name != name or row.target_sec_uid not in ('', sec_uid):
                        continue
                    snapshot = db.get(TaskRunRecipient, (run_id, recipient['position'])) if run_id else None
                    if run_id and (snapshot is None or snapshot.account_id != account_id
                                   or snapshot.target_name != name or snapshot.target_sec_uid not in ('', sec_uid)):
                        continue
                    row.target_sec_uid = sec_uid
                    if snapshot is not None:
                        snapshot.target_sec_uid = sec_uid
                    approvals = name_approvals(db, account_id, sec_uid, name)
                    resolved.append((recipient, sec_uid, approvals))
            # Do not let a failed commit leave in-memory identity ahead of the
            # durable task and run snapshot used by retries.
            for recipient, sec_uid, approvals in resolved:
                recipient['target_sec_uid'] = sec_uid
                recipient['name_approvals'] = approvals
        except Exception:
            # Enrichment is an optimisation; search remains the safe fallback.
            pass

    async def _run_batch(self, run_id, task_id, account_id, cookies, credential_version, recipients, now):
        from spark_console.message_content import render_content, parse_content
        from spark_console.models import RunMessageContent, TaskRunRecipient
        self._enrich_recipient_sec_uids(recipients, task_id, account_id, run_id=run_id)
        # Resolve external content once, outside a DB write transaction and before
        # opening the browser. The committed text is reused by all retry paths.
        for recipient in recipients:
            if recipient['message_template']:
                continue
            with session_scope(self.engine) as db:
                row = db.get(TaskRunRecipient, (run_id, recipient['position']))
                template = row.message_template
                scheduled = db.get(TaskRun, run_id).scheduled_for
            try:
                text, source = await asyncio.wait_for(asyncio.to_thread(
                    render_content, template, scheduled, cache_dir=self.settings.data_dir), 6)
            except Exception:
                text, source = parse_content(template).get('fallback', '今日火花，祝你今天开心！'), '一言不可用，已使用备用文案'
            with session_scope(self.engine) as db:
                content = db.get(RunMessageContent, (run_id, recipient['position']))
                if not content.text:
                    content.text, content.source = text, source
                recipient['message_template'] = content.text
        stopped_for_auth = False
        denied = False

        async def before_send(index):
            nonlocal denied
            with session_scope(self.engine) as db:
                allowed = BatchRunService(db).before_send(run_id, recipients[index]["position"])
                denied = denied or not allowed
                return allowed

        async def on_result(index, result):
            nonlocal stopped_for_auth, denied
            with session_scope(self.engine) as db:
                BatchRunService(db).record(run_id, recipients[index]["position"], result)
                if result.success and not stopped_for_auth:
                    db.execute(update(DouyinAccount).where(DouyinAccount.id == account_id).values(
                        validation_state="valid", last_verified_at=datetime.now(timezone.utc),
                        invalidated_at=None, invalid_reason_code=None, auth_incident_id=None,
                    ))
                if result.error_code in {"login_expired", "cookie_invalid"}:
                    stopped_for_auth = True
                    self._record_auth_incident(db, db.get(DouyinAccount, account_id),
                                               result.error_code, datetime.now(timezone.utc))
                if result.error_code == "authorization_ended":
                    denied = True

        try:
            if recipients:
                # Keep the existing three-minute window within the four-minute spacing.
                with trace_run(self.engine, run_id):
                    await asyncio.wait_for(self.executor.execute_batch(
                        cookies, recipients, before_send=before_send, on_result=on_result,
                        credential_version=credential_version), timeout=self.execution_timeout_seconds)
        except Exception:
            # Durable checkpoints decide what is safe, not the exception type.
            pass
        with session_scope(self.engine) as db:
            batch = BatchRunService(db)
            batch.interrupt(run_id, retry_pending=not (stopped_for_auth or denied))
            status, summary, retryable = batch.summary(run_id)
            run, task = db.get(TaskRun, run_id), db.get(SparkTask, task_id)
            if retryable and task and task.enabled and not stopped_for_auth and not denied:
                rows_preview = batch.rows(run_id)
                unsent = [r for r in rows_preview if r.status not in ("success", "submitted")]
                selection_only = bool(unsent) and all(
                    r.stage == "selecting_target" for r in unsent
                )
                scheduled = self._schedule_retry(db, task, run, datetime.now(timezone.utc), "batch_retry", long_spacing=selection_only)
                if scheduled is not None:
                    batch.remember_retry(task_id, run_id, task.next_run_at)
                    scheduled.error_summary = summary + "；仅重试明确未发送的好友"
                    return scheduled
            rows = batch.rows(run_id)
            chat_unavailable = bool(rows) and all(
                row.error_code == "chat_ui_unavailable" for row in rows
            )
            if chat_unavailable:
                self._record_auth_incident(
                    db,
                    db.get(DouyinAccount, account_id),
                    "chat_ui_unavailable",
                    datetime.now(timezone.utc),
                )
                summary = "聊天页连续无法加载，账号已暂停，请重新绑定后恢复任务"
            missing = [r.target_name for r in rows if r.error_code == "target_not_found"]
            finished = finish_run(run, status, "batch_complete", datetime.now(timezone.utc),
                                 "chat_ui_unavailable" if chat_unavailable else
                                 "target_not_found" if missing else None if status == "success" else "batch_partial", summary)
            if status in {"failed", "partial"}:
                self._record_task_failure_incident(db, task, finished, datetime.now(timezone.utc),
                                                  target_label="、".join(missing)[:120] if missing else None)
                self._pause_stuck_selection_recipients(db, task, datetime.now(timezone.utc))
            return finished

    def _pause_stuck_selection_recipients(self, db, task, now: datetime) -> None:
        # A friend whose selection fails three consecutive nights keeps
        # flipping a nightly coin; remove it and tell the owner to rename and
        # re-add instead of retrying forever.
        from zoneinfo import ZoneInfo
        from spark_console.models import (
            NotificationPreference,
            SparkTaskRecipient,
            TaskRunRecipient,
            User,
        )
        from spark_console.services.notifications import NotificationService
        zone = ZoneInfo("Asia/Shanghai")
        rows = db.execute(
            select(
                TaskRunRecipient.position,
                TaskRunRecipient.status,
                TaskRunRecipient.stage,
                TaskRun.scheduled_for,
                TaskRunRecipient.account_id,
                TaskRunRecipient.target_sec_uid,
                TaskRunRecipient.target_name,
            )
            .join(TaskRun, TaskRun.id == TaskRunRecipient.run_id)
            .where(
                TaskRun.task_id == task.id,
                TaskRun.scheduled_for >= now - timedelta(days=4),
            )
            .order_by(TaskRun.scheduled_for.asc())
        ).all()
        current_recipients = {r.position: r for r in db.scalars(
            select(SparkTaskRecipient).where(SparkTaskRecipient.task_id == task.id)).all()}
        by_position: dict = {}
        for position, row_status, row_stage, scheduled_for, account_id, sec_uid, target_name in rows:
            current = current_recipients.get(position)
            if current is None or account_id != task.douyin_account_id:
                continue
            if current.target_sec_uid:
                if sec_uid != current.target_sec_uid:
                    continue
            elif sec_uid or target_name != current.target_name:
                continue
            if scheduled_for.tzinfo is None:
                scheduled_for = scheduled_for.replace(tzinfo=timezone.utc)
            day = scheduled_for.astimezone(zone).date()
            by_position.setdefault(position, {})[day] = (row_status, row_stage)
        today = now.astimezone(zone).date()
        for position in sorted(by_position):
            days = by_position[position]
            stuck = True
            for back in range(3):
                outcome = days.get(today - timedelta(days=back))
                if outcome is None:
                    stuck = False
                    break
                status_value, stage_value = outcome
                if status_value in ("success", "submitted") or stage_value != "selecting_target":
                    stuck = False
                    break
            if not stuck:
                continue
            recipient = db.get(
                SparkTaskRecipient, {"task_id": task.id, "position": position}
            )
            name = recipient.target_name if recipient is not None else ""
            if recipient is not None:
                remaining = db.scalars(select(SparkTaskRecipient).where(
                    SparkTaskRecipient.task_id == task.id)).all()
                if len(remaining) == 1:
                    # Keep the batch marker when the last recipient fails; a
                    # legacy fallback must never resurrect a removed target.
                    task.enabled = False
                    task.next_run_at = None
                else:
                    db.delete(recipient)
                db.flush()
            AuditService(db).write(
                task.owner_user_id,
                "task.recipient.auto_paused",
                "spark_task",
                task.id,
            )
            user = db.get(User, task.owner_user_id)
            preference = db.get(NotificationPreference, task.owner_user_id)
            if (
                user is None
                or preference is None
                or not user.email_verified_at
                or not user.email_ciphertext
                or not user.email_nonce
            ):
                continue
            email = self.pii.decrypt_email(
                user.email_ciphertext,
                user.email_nonce,
                aad=f"user:{user.id}".encode(),
            )
            NotificationService(db, self.pii, AuditService(db)).enqueue_template(
                task.owner_user_id,
                "task_recipient_paused",
                email,
                "task_failure",
                {
                    "target_name": name or "某位好友",
                    "reason": "该好友已连续 3 晚无法在抖音中唯一选中；已从任务中移除，或在只剩一位好友时暂停整个任务。请设置独特的备注名后，编辑并重新启用任务。",
                    "action_path": f"/tasks/{task.id}/edit",
                },
                f"task-recipient-paused:{task.id}:{position}:{today.isoformat()}",
                now=now,
            )

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
        target_label: str | None = None,
    ) -> None:
        if self.pii is None or run.status not in {"failed", "partial"}:
            return
        recent = db.scalars(
            select(TaskRun)
            .where(TaskRun.task_id == task.id)
            .order_by(TaskRun.scheduled_for.desc(), TaskRun.id.desc())
        ).all()
        streak = []
        for candidate in recent:
            if candidate.status not in {"failed", "partial"}:
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
                "target_name": target_label or task.target_name,
                "reason": reason,
                "action_path": action_path,
            },
            f"{incident_key}:email",
            now=now,
        )

    def _schedule_retry(self, db, task, run, now, stage, long_spacing=False):
        if long_spacing:
            # Selection-stage failures repeat identically all night: the
            # search page state is sticky within minutes. One later same-day
            # retry gets a fresh window instead of burning attempts 1m/5m
            # apart; the 12h dedupe window caps it at one per night.
            delays = (timedelta(minutes=25),)
            window = timedelta(hours=12)
        else:
            delays = self.RETRY_DELAYS
            window = timedelta(minutes=15)
        retry_codes = tuple(
            f"retry_scheduled_{int(delay.total_seconds() // 60)}m"
            for delay in delays
        )
        used_codes = set(
            db.scalars(
                select(TaskRun.error_code).where(
                    TaskRun.task_id == task.id,
                    TaskRun.started_at >= now - window,
                    TaskRun.error_code.in_(retry_codes),
                )
            ).all()
        )
        for delay, code in zip(delays, retry_codes):
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
    from spark_console.browser_runtime import run_supervisor
    run_supervisor('worker')
