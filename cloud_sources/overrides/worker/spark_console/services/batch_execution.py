"""Durable per-recipient checkpoints. The caller commits each send boundary."""
from datetime import datetime, timezone
import json

from sqlalchemy import select

from spark_console.models import (
    DouyinAccount, SparkTask, SparkTaskRecipient, TaskRun, TaskRunRecipient, TaskBatchRetry, ManualRunRetry,
    RecipientCheckEvidence,
)
from spark_console.services.audits import AuditService
from spark_console.services.task_capacity import TaskCapacityService
from spark_console.services.recipient_review import name_approvals, save_evidence


class BatchRunService:
    def __init__(self, session):
        self.session = session

    def rows(self, run_id):
        return list(self.session.scalars(select(TaskRunRecipient).where(
            TaskRunRecipient.run_id == run_id
        ).order_by(TaskRunRecipient.position)).all())

    def prepare(self, run, task):
        retry = self.session.get(TaskBatchRetry, task.id)
        source = self.rows(retry.source_run_id) if retry else []
        manual = self.session.get(ManualRunRetry, retry.source_run_id) if retry else None
        if retry:
            def utc(value):
                return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
            # Retry identity is the scheduled occurrence, not its calendar day.
            if utc(retry.scheduled_for) != utc(run.scheduled_for):
                source = []
        if source and any(r.account_id != task.douyin_account_id for r in source):
            source = []
        if not source:
            source = self.session.scalars(select(SparkTaskRecipient).where(
                SparkTaskRecipient.task_id == task.id
            ).order_by(SparkTaskRecipient.position)).all()
        for item in source:
            pending = not isinstance(item, TaskRunRecipient) or item.status == "pending" or item.retryable
            if manual and isinstance(item, TaskRunRecipient):
                pending = item.position in json.loads(manual.recipient_positions) and item.status in {'pending', 'failed'}
            row = TaskRunRecipient(
                run_id=run.id, position=item.position, account_id=task.douyin_account_id,
                target_name=item.target_name, target_sec_uid=item.target_sec_uid,
                message_template=item.message_template,
                status="pending" if pending else item.status,
                stage="queued" if pending else item.stage,
                error_code=None if pending else item.error_code,
                error_summary=None if pending else item.error_summary,
                finished_at=None if pending else item.finished_at,
            )
            self.session.add(row)
            if pending and isinstance(item, TaskRunRecipient) and not manual:
                prior = self.session.get(RecipientCheckEvidence, (item.run_id, item.position))
                if prior:
                    self.session.add(RecipientCheckEvidence(run_id=run.id, position=item.position,
                        diagnostic_json='{}', attempt_number=prior.attempt_number + 1))
        if retry:
            self.session.delete(retry)
        self.session.flush()
        return [dict(position=r.position, target_name=r.target_name, target_sec_uid=r.target_sec_uid,
                     message_template=r.message_template,
                     name_approvals=name_approvals(self.session, r.account_id, r.target_sec_uid, r.target_name))
                for r in self.rows(run.id) if r.status == "pending"]

    def before_send(self, run_id, position, at=None):
        now = at or datetime.now(timezone.utc)
        row = self.session.get(TaskRunRecipient, (run_id, position))
        run = self.session.get(TaskRun, run_id)
        task = self.session.get(SparkTask, run.task_id) if run else None
        account = self.session.get(DouyinAccount, task.douyin_account_id) if task and task.douyin_account_id else None
        if not (row and row.status == "pending" and task and task.enabled and account
                and account.id == row.account_id and account.validation_state != "invalid"
                and TaskCapacityService(self.session, AuditService(self.session)).task_authorized(task, now)):
            return False
        row.status = "sending"
        row.stage = "sending"
        self.session.flush()
        return True

    def record(self, run_id, position, result, at=None):
        row = self.session.get(TaskRunRecipient, (run_id, position))
        row.status = ("submitted" if result.stage == "submitted" else "success") if result.success else (
            "uncertain" if result.stage in {"sending", "confirming"} else "failed")
        row.stage = result.stage
        row.error_code = result.error_code
        row.error_summary = (result.error_summary or "")[:240] or None
        row.retryable = result.retryable and row.status == "failed"
        row.finished_at = at or datetime.now(timezone.utc)
        save_evidence(self.session, row, getattr(result, 'recipient_diagnostic', None))
        self.session.flush()
        evidence = self.session.get(RecipientCheckEvidence, (run_id, position))
        if result.error_code == 'recipient_name_unverified' and evidence and evidence.attempt_number >= 3:
            row.retryable = False
            row.error_summary = '名称检查已达到本轮自动重试上限；请查看诊断并人工确认后补跑'
            self.session.flush()

    def interrupt(self, run_id, at=None, retry_pending=True):
        for row in self.rows(run_id):
            if row.status == "sending":
                row.status, row.stage = "uncertain", "sending"
                row.error_code = "delivery_uncertain"
                row.error_summary = "发送中断，结果不明；不会自动重发"
                row.retryable = False
            elif row.status == "pending":
                row.status, row.stage = "failed", "not_started"
                row.error_code = "batch_not_started"
                row.error_summary = "本批中断，此好友尚未发送"
                row.retryable = retry_pending
            else:
                continue
            row.finished_at = at or datetime.now(timezone.utc)
        self.session.flush()

    def remember_retry(self, task_id, run_id, scheduled_for):
        link = self.session.get(TaskBatchRetry, task_id)
        if link:
            link.source_run_id = run_id
            link.scheduled_for = scheduled_for
        else:
            self.session.add(TaskBatchRetry(task_id=task_id, source_run_id=run_id, scheduled_for=scheduled_for))
        self.session.flush()

    def summary(self, run_id):
        rows = self.rows(run_id)
        success = sum(r.status in {"success", "submitted"} for r in rows)
        uncertain = sum(r.status == "uncertain" for r in rows)
        retryable = any(r.retryable for r in rows)
        status = "success" if rows and success == len(rows) else "partial" if success else "failed"
        return status, f"共 {len(rows)} 人，成功/已提交 {success} 人，待核实 {uncertain} 人，其他 {len(rows)-success-uncertain} 人", retryable
