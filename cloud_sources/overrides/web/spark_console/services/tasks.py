from __future__ import annotations

import re
from functools import wraps
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from spark_console.models import (
    DouyinContactIdentity,
    SparkTask,
    SparkTaskTargetIdentity,
    SparkTaskRecipient,
    TaskBatchRetry,
    TaskRun,
    User,
)
from spark_console.services import Conflict, NotFound, ValidationError
from spark_console.services.accounts import AccountService
from spark_console.services.audits import AuditService
from spark_console.services.task_capacity import TaskCapacityService


_TIME_RE = re.compile(r"^(?:[01]\d|2[0-3]):[0-5]\d$")
_RELOGIN_RETRY_STAGES = ("authenticating", "selecting_target")
_RELOGIN_RETRY_NOTE = "重新登录成功，已安排自动补跑"


def atomic_task_write(method):
    @wraps(method)
    def wrapped(self, *args, **kwargs):
        with self.session.begin_nested():
            return method(self, *args, **kwargs)
    return wrapped


def schedule_recent_safe_failures(
    session: Session,
    account_id: str,
    *,
    now: datetime | None = None,
    window: timedelta = timedelta(hours=2),
    delay: timedelta = timedelta(minutes=1),
) -> list[str]:
    current = now or datetime.now(timezone.utc)
    cutoff = current - window
    failures = session.scalars(
        select(TaskRun)
        .join(SparkTask, SparkTask.id == TaskRun.task_id)
        .where(
            SparkTask.douyin_account_id == account_id,
            SparkTask.enabled.is_(True),
            TaskRun.status == "failed",
            TaskRun.stage.in_(_RELOGIN_RETRY_STAGES),
            TaskRun.finished_at.is_not(None),
            TaskRun.finished_at >= cutoff,
        )
        .order_by(TaskRun.finished_at.desc(), TaskRun.id.desc())
    ).all()
    scheduled = []
    seen_task_ids = set()
    for failure in failures:
        if failure.task_id in seen_task_ids:
            continue
        seen_task_ids.add(failure.task_id)
        if _RELOGIN_RETRY_NOTE in (failure.error_summary or ""):
            continue
        later_success = session.scalar(
            select(TaskRun.id)
            .where(
                TaskRun.task_id == failure.task_id,
                TaskRun.status == "success",
                TaskRun.scheduled_for > failure.scheduled_for,
            )
            .limit(1)
        )
        if later_success is not None:
            continue
        task = session.get(SparkTask, failure.task_id)
        if task is None or not task.enabled:
            continue
        task.next_run_at = TaskCapacityService(
            session, AuditService(session)
        ).next_available_run_at(current + delay, task.id)
        failure.error_summary = (
            f"{failure.error_summary}；{_RELOGIN_RETRY_NOTE}"
            if failure.error_summary
            else _RELOGIN_RETRY_NOTE
        )[:240]
        scheduled.append(task.id)
    session.flush()
    return scheduled


class TaskService:
    def __init__(self, session: Session, accounts: AccountService, audit: AuditService):
        self.session = session
        self.accounts = accounts
        self.audit = audit

    def recipients_for(self, task: SparkTask) -> list[dict]:
        rows = self.session.scalars(select(SparkTaskRecipient).where(
            SparkTaskRecipient.task_id == task.id
        ).order_by(SparkTaskRecipient.position)).all()
        if rows:
            return [dict(target_name=r.target_name, target_sec_uid=r.target_sec_uid,
                         message_template=r.message_template) for r in rows]
        identity = self.session.get(SparkTaskTargetIdentity, task.id)
        return [dict(target_name=task.target_name,
                     target_sec_uid=identity.sec_uid if identity else "",
                     message_template=task.message_template)]

    def validate_recipients(self, owner_id, account_id, recipients) -> list[dict]:
        self.accounts.get_owned(owner_id, account_id)
        if not isinstance(recipients, list) or not 1 <= len(recipients) <= 5:
            raise ValidationError("每个任务请选择 1–5 位好友")
        clean, seen_ids, seen_names = [], set(), set()
        for value in recipients:
            if not isinstance(value, dict):
                raise ValidationError("好友信息格式无效")
            fields = [value.get(key, "") for key in ("target_name", "target_sec_uid", "message_template")]
            if not all(isinstance(field, str) for field in fields):
                raise ValidationError("好友信息格式无效")
            name, uid, message = (field.strip() for field in fields)
            if not 1 <= len(name) <= 64 or not 1 <= len(message) <= 500 or len(uid) > 256:
                raise ValidationError("每位好友名称须为 1–64 字，专属消息须为 1–500 字")
            if uid and self.session.get(DouyinContactIdentity, (account_id, uid)) is None:
                raise ValidationError("所选好友不属于当前抖音账号")
            if name in seen_names or (uid and uid in seen_ids):
                raise ValidationError("同一任务不能重复选择好友")
            seen_names.add(name)
            if uid:
                seen_ids.add(uid)
            clean.append(dict(target_name=name, target_sec_uid=uid, message_template=message))
        return clean

    def assert_no_overlap(self, account_id, recipients, exclude_task_id=None):
        others = self.session.scalars(select(SparkTask).where(
            SparkTask.douyin_account_id == account_id, SparkTask.enabled.is_(True)
        )).all()
        for other in others:
            if other.id == exclude_task_id:
                continue
            for prior in self.recipients_for(other):
                for item in recipients:
                    if ((item['target_sec_uid'] and item['target_sec_uid'] == prior['target_sec_uid'])
                            or item['target_name'] == prior['target_name']):
                        raise ValidationError("该好友已在同一账号的其他启用任务中，请先暂停或移除")

    def _save_recipients(self, task, recipients):
        for row in self.session.scalars(select(SparkTaskRecipient).where(SparkTaskRecipient.task_id == task.id)).all():
            self.session.delete(row)
        self.session.flush()
        for position, item in enumerate(recipients):
            self.session.add(SparkTaskRecipient(task_id=task.id, position=position, **item))
        retry = self.session.get(TaskBatchRetry, task.id)
        if retry:
            self.session.delete(retry)
        self.session.flush()

    def assert_not_running(self, task):
        if self.session.scalar(select(TaskRun.id).where(
            TaskRun.task_id == task.id, TaskRun.status == "running", TaskRun.finished_at.is_(None)
        ).limit(1)):
            raise ValidationError("任务正在执行，请待本批结束后再编辑或删除")

    def get_owned(self, owner_id: str, task_id: str) -> SparkTask:
        task = self.session.scalar(
            select(SparkTask).where(
                SparkTask.id == task_id, SparkTask.owner_user_id == owner_id
            )
        )
        if task is None:
            raise NotFound("task not found")
        return task

    def list_owned(self, owner_id: str) -> list[SparkTask]:
        return list(
            self.session.scalars(
                select(SparkTask)
                .where(SparkTask.owner_user_id == owner_id)
                .order_by(SparkTask.send_time, SparkTask.created_at)
            ).all()
        )

    @atomic_task_write
    def create(
        self,
        owner_id: str,
        account_id: str,
        target_name: str,
        send_time: str,
        message_template: str,
        target_sec_uid: str | None = None,
        recipients: list[dict] | None = None,
    ) -> SparkTask:
        if recipients is not None:
            recipients = self.validate_recipients(owner_id, account_id, recipients)
            self.assert_no_overlap(account_id, recipients)
            target_name, target_sec_uid, message_template = (
                recipients[0][key] for key in ("target_name", "target_sec_uid", "message_template")
            )
        target = target_name.strip()
        message = message_template.strip()
        if not target or len(target) > 64:
            raise ValidationError("好友名称须为 1–64 个字符")
        if not _TIME_RE.fullmatch(send_time):
            raise ValidationError("发送时间格式必须为 HH:MM")
        if not message or len(message) > 500:
            raise ValidationError("消息内容须为 1–500 个字符")
        account = self.accounts.get_owned(owner_id, account_id)
        owner = self.session.get(User, owner_id)
        if owner is None:
            raise NotFound("user not found")
        capacity = TaskCapacityService(self.session, self.audit)
        capacity.assert_can_create(owner)
        capacity.assert_slot_available(send_time)
        stable_target = str(target_sec_uid or "").strip()
        if stable_target and self.session.get(
            DouyinContactIdentity, (account.id, stable_target)
        ) is None:
            raise ValidationError("所选好友不属于当前抖音账号")
        local_now = datetime.now(ZoneInfo("Asia/Shanghai"))
        hour, minute = map(int, send_time.split(":"))
        candidate = local_now.replace(hour=hour, minute=minute, second=0, microsecond=0)
        if candidate <= local_now:
            from datetime import timedelta
            candidate += timedelta(days=1)
        task = SparkTask(
            owner_user_id=owner_id,
            douyin_account_id=account.id,
            target_name=target,
            send_time=send_time,
            message_template=message,
            enabled=True,
            next_run_at=candidate.astimezone(timezone.utc),
        )
        self.session.add(task)
        try:
            self.session.flush()
            if stable_target:
                self.session.add(
                    SparkTaskTargetIdentity(
                        task_id=task.id,
                        sec_uid=stable_target,
                    )
                )
                self.session.flush()
        except IntegrityError as error:
            raise Conflict("相同账号、好友和时间的启用任务已存在") from error
        self.audit.write(owner_id, "task.created", "spark_task", task.id)
        if recipients is not None:
            self._save_recipients(task, recipients)
            capacity.bind_task(task)
        return task

    def set_enabled_owned(self, owner_id: str, task_id: str, enabled: bool) -> SparkTask:
        task = self.get_owned(owner_id, task_id)
        return self.set_enabled(task, enabled, owner_id)

    @atomic_task_write
    def set_enabled(
        self, task: SparkTask, enabled: bool, actor_id: str
    ) -> SparkTask:
        if enabled and task.douyin_account_id is None:
            raise ValidationError("账号已删除，无法启用任务")
        if enabled and not task.enabled:
            capacity = TaskCapacityService(self.session, self.audit)
            owner = self.session.get(User, task.owner_user_id)
            if owner is None:
                raise NotFound("user not found")
            if owner.status != 'active':
                raise ValidationError('用户已停用，无法启用任务')
            capacity.assert_can_enable(owner)
            capacity.assert_slot_available(task.send_time, task.id)
            if self.session.get(SparkTaskRecipient, (task.id, 0)):
                self.assert_no_overlap(task.douyin_account_id, self.recipients_for(task), task.id)
                capacity.bind_task(task)
            from spark_console.scheduler import compute_next_run
            task.next_run_at = compute_next_run(task.send_time, datetime.now(timezone.utc))
        task.enabled = enabled
        self.audit.write(actor_id, "task.enabled" if enabled else "task.disabled", "spark_task", task.id)
        return task

    @atomic_task_write
    def update_owned(
        self,
        owner_id: str,
        task_id: str,
        account_id: str,
        target_name: str,
        send_time: str,
        message_template: str,
        target_sec_uid: str | None = None,
        recipients: list[dict] | None = None,
    ) -> SparkTask:
        task = self.get_owned(owner_id, task_id)
        self.assert_not_running(task)
        if recipients is None and self.session.get(SparkTaskRecipient, (task.id, 0)):
            if self.session.get(SparkTaskRecipient, (task.id, 1)):
                raise ValidationError("多好友任务必须提交完整好友列表")
            recipients = [dict(target_name=target_name, target_sec_uid=target_sec_uid or "", message_template=message_template)]
        if recipients is not None:
            recipients = self.validate_recipients(owner_id, account_id, recipients)
            if task.enabled:
                self.assert_no_overlap(account_id, recipients, task.id)
            target_name, target_sec_uid, message_template = (
                recipients[0][key] for key in ("target_name", "target_sec_uid", "message_template")
            )
        target = target_name.strip()
        message = message_template.strip()
        if not target or len(target) > 64:
            raise ValidationError("好友名称须为 1–64 个字符")
        if not _TIME_RE.fullmatch(send_time):
            raise ValidationError("发送时间格式必须为 HH:MM")
        if not message or len(message) > 500:
            raise ValidationError("消息内容须为 1–500 个字符")
        account = self.accounts.get_owned(owner_id, account_id)
        stable_target = str(target_sec_uid or "").strip()
        if stable_target and self.session.get(
            DouyinContactIdentity, (account.id, stable_target)
        ) is None:
            raise ValidationError("所选好友不属于当前抖音账号")
        if task.enabled:
            TaskCapacityService(self.session, self.audit).assert_slot_available(
                send_time, task.id
            )
            duplicate = self.session.scalar(
                select(SparkTask.id).where(
                    SparkTask.id != task.id,
                    SparkTask.douyin_account_id == account.id,
                    SparkTask.target_name == target,
                    SparkTask.send_time == send_time,
                    SparkTask.enabled.is_(True),
                )
            )
            if duplicate is not None:
                raise Conflict("相同账号、好友和时间的启用任务已存在")

        local_now = datetime.now(ZoneInfo("Asia/Shanghai"))
        hour, minute = map(int, send_time.split(":"))
        candidate = local_now.replace(
            hour=hour, minute=minute, second=0, microsecond=0
        )
        if candidate <= local_now:
            from datetime import timedelta

            candidate += timedelta(days=1)
        task.douyin_account_id = account.id
        task.target_name = target
        task.send_time = send_time
        task.message_template = message
        task.next_run_at = candidate.astimezone(timezone.utc)

        binding = self.session.get(SparkTaskTargetIdentity, task.id)
        if stable_target:
            if binding is None:
                self.session.add(
                    SparkTaskTargetIdentity(task_id=task.id, sec_uid=stable_target)
                )
            else:
                binding.sec_uid = stable_target
        elif binding is not None:
            self.session.delete(binding)
        self.audit.write(owner_id, "task.updated", "spark_task", task.id)
        if recipients is not None:
            self._save_recipients(task, recipients)
            if task.enabled:
                TaskCapacityService(self.session, self.audit).bind_task(task)
        return task

    def delete_owned(self, owner_id: str, task_id: str) -> None:
        task = self.get_owned(owner_id, task_id)
        self.assert_not_running(task)
        self.session.delete(task)
        self.audit.write(owner_id, "task.deleted", "spark_task", task_id)
