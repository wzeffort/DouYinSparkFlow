"""Admin-approved retries of today's latest, definitely-unsent results.

Admission is committed with next_run_at and the existing batch retry link.
Callers serialize scheduling with the database write transaction.
"""
import json
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from sqlalchemy import select

from spark_console.models import DouyinAccount, ManualRunRetry, SparkTask, SparkTaskRecipient, TaskBatchRetry, TaskRun, User
from spark_console.services import NotFound, ValidationError
from spark_console.services.audits import AuditService
from spark_console.services.batch_execution import BatchRunService
from spark_console.services.task_capacity import TaskCapacityService

SHANGHAI = ZoneInfo('Asia/Shanghai')
SAFE_CODES = {'network_unavailable', 'conversation_not_opened', 'target_not_found',
    'recipient_name_unverified', 'recipient_identity_unverified', 'target_name_ambiguous',
    'chat_load_failed', 'chat_ui_unavailable', 'login_expired', 'cookie_invalid', 'batch_not_started'}
SAFE_STAGES = {'starting', 'authenticating', 'selecting_target', 'not_started', 'queued', 'navigation', 'target_search'}


def utc(value):
    return value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value.astimezone(timezone.utc)


class ManualRetryService:
    def __init__(self, db, now=None):
        self.db = db
        self.now = utc(now or datetime.now(timezone.utc))
        self.start = self.now.astimezone(SHANGHAI).replace(hour=0, minute=0, second=0, microsecond=0).astimezone(timezone.utc)
        self.end = self.start + timedelta(days=1)
        self.day = self.start.astimezone(SHANGHAI).date().isoformat()

    def require_admin(self, actor_id):
        actor = self.db.get(User, actor_id)
        if not actor or actor.role != 'admin' or actor.status != 'active':
            raise NotFound('admin required')

    def _entry(self, run):
        task = self.db.get(SparkTask, run.task_id)
        owner = self.db.get(User, task.owner_user_id) if task else None
        rows = BatchRunService(self.db).rows(run.id)
        positions = [r.position for r in rows if (r.status == 'failed' and r.stage in SAFE_STAGES and r.error_code in SAFE_CODES)
            or (r.status == 'pending' and r.stage in {'queued','not_started'})]
        reason = ''
        latest = self.db.scalar(select(TaskRun.id).where(TaskRun.task_id == run.task_id)
            .order_by(TaskRun.scheduled_for.desc(), TaskRun.id.desc()).limit(1))
        account = self.db.get(DouyinAccount, task.douyin_account_id) if task and task.douyin_account_id else None
        if not self.start <= utc(run.scheduled_for) < self.end:
            reason = '不是今天的执行记录'
        elif latest != run.id:
            reason = '已有更新的执行结果'
        elif self.db.get(ManualRunRetry, run.id):
            reason = '这条记录已经安排过重跑'
        elif run.status not in {'failed','partial'}:
            reason = '不是失败或部分完成记录'
        elif not task or not task.enabled or not owner or owner.status != 'active':
            reason = '任务或用户已暂停'
        elif not account or account.owner_user_id != task.owner_user_id or account.validation_state != 'valid':
            reason = '请先重新登录并验证抖音账号'
        elif not TaskCapacityService(self.db, AuditService(self.db)).task_authorized(task, self.now):
            reason = '额度已过期或不可用'
        elif self.db.scalar(select(TaskRun.id).where(TaskRun.task_id == task.id, TaskRun.status.in_(['running','queued'])).limit(1)):
            reason = '任务正在执行或排队'
        elif self.db.get(TaskBatchRetry, task.id) or (task.next_run_at and utc(task.next_run_at) < self.end):
            reason = '今天已有待执行计划'
        elif rows and not positions:
            reason = '没有明确未发送的好友'
        elif not rows and not (run.error_code in SAFE_CODES and run.stage in SAFE_STAGES):
            reason = '无法确认未发送，需人工核实'
        if not reason and rows:
            current = list(self.db.scalars(select(SparkTaskRecipient).where(SparkTaskRecipient.task_id == task.id).order_by(SparkTaskRecipient.position)))
            snapshot = lambda r: (r.position, r.target_sec_uid, r.target_name, r.message_template)
            if any(r.account_id != task.douyin_account_id for r in rows) or [snapshot(r) for r in current] != [snapshot(r) for r in rows]:
                reason = '好友或消息已修改，请按当前任务计划执行'
        return dict(run_id=run.id, task_id=run.task_id, username=owner.username if owner else '已删除用户',
            target_name=task.target_name if task else '已删除任务', eligible=not reason, reason=reason,
            recipient_count=len(positions) if rows else 1, preserved_count=len(rows)-len(positions),
            positions=positions, scheduled_for=None)

    def preview(self, actor_id):
        self.require_admin(actor_id)
        runs = self.db.scalars(select(TaskRun).where(TaskRun.scheduled_for >= self.start,
            TaskRun.scheduled_for < self.end, TaskRun.status.in_(['failed','partial']))
            .order_by(TaskRun.scheduled_for.desc(), TaskRun.id.desc())).all()
        seen, entries = set(), []
        for run in runs:
            if run.task_id not in seen:
                seen.add(run.task_id); entries.append(self._entry(run))
        return entries

    def schedule(self, actor_id, run_ids, *, owned=False, positions=None):
        if not owned:
            self.require_admin(actor_id)
        actor = self.db.get(User, actor_id)
        if not actor or actor.status != 'active':
            raise NotFound('user required')
        results = []
        capacity = TaskCapacityService(self.db, AuditService(self.db))
        for run_id in sorted(set(run_ids)):
            run = self.db.get(TaskRun, run_id)
            if run is None:
                continue
            task = self.db.get(SparkTask, run.task_id)
            if owned and actor.role != 'admin' and task.owner_user_id != actor_id:
                raise NotFound('执行记录不存在')
            entry = self._entry(run)
            if positions is not None:
                selected = sorted(set(positions))
                if not selected or not set(selected).issubset(entry['positions']):
                    entry.update(eligible=False, reason='所选好友不是明确未发送状态')
                else:
                    entry['positions'] = selected
                    rows = BatchRunService(self.db).rows(run.id)
                    entry['recipient_count'] = len(selected)
                    entry['preserved_count'] = len(rows) - len(selected)
            if entry['eligible']:
                task = self.db.get(SparkTask, run.task_id)
                try:
                    candidate = capacity.next_available_run_at(self.now + timedelta(minutes=1), task.id)
                    if candidate >= self.end:
                        raise ValidationError('今天已没有空闲时段')
                    if not capacity.task_authorized(task, candidate):
                        raise ValidationError('额度将在重跑前到期')
                except ValidationError as error:
                    entry.update(eligible=False, reason=str(error)); results.append(entry); continue
                self.db.add(ManualRunRetry(source_run_id=run.id, task_id=task.id, actor_user_id=actor_id,
                    scheduled_for=candidate, recipient_positions=json.dumps(entry['positions'])))
                task.next_run_at = candidate
                if entry['positions']:
                    BatchRunService(self.db).remember_retry(task.id, run.id, candidate)
                AuditService(self.db).write(actor_id, 'task.manual_retry_scheduled', 'task_run', run.id,
                    detail=f"day={self.day};recipients={entry['recipient_count']};scheduled_for={candidate.isoformat()}")
                self.db.flush()
                entry['scheduled_for'] = candidate
            results.append(entry)
        return results
