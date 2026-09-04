from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from math import ceil
from zoneinfo import ZoneInfo

from sqlalchemy import func, or_, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from spark_console.models import (
    SparkTask,
    TaskQuotaGrant,
    TaskQuotaPolicy,
    TaskRun,
    User,
    UserTaskQuota,
)
from spark_console.services import NotFound, ValidationError
from spark_console.services.audits import AuditService


MIN_TASK_LIMIT = 0
MAX_TASK_LIMIT = 100
MIN_SAVED_TASKS = 1
MAX_SAVED_TASKS = 500
SLOT_MINUTES = 4
MINUTES_PER_DAY = 24 * 60
SHANGHAI = ZoneInfo("Asia/Shanghai")


@dataclass(frozen=True)
class SlotAvailability:
    available: bool
    remaining: int
    suggestions: tuple[str, ...]


class TaskCapacityService:
    def __init__(self, session: Session, audit: AuditService):
        self.session = session
        self.audit = audit

    @staticmethod
    def _aware(value: datetime) -> datetime:
        return value if value.tzinfo else value.replace(tzinfo=timezone.utc)

    def policy(self) -> TaskQuotaPolicy:
        policy = self.session.get(TaskQuotaPolicy, 1)
        if policy is None:
            raise ValidationError("任务额度策略尚未初始化")
        return policy

    def bootstrap_user(
        self,
        user: User,
        use_current_policy: bool = False,
        effective_at: datetime | None = None,
    ) -> TaskQuotaGrant | None:
        if user.role == "admin":
            return None
        existing = self.session.scalar(
            select(TaskQuotaGrant.id).where(TaskQuotaGrant.user_id == user.id).limit(1)
        )
        if existing is not None:
            return None
        policy = self.policy()
        legacy = self.session.get(UserTaskQuota, user.id)
        amount = legacy.task_limit if legacy is not None else policy.default_amount
        current = self._aware(effective_at or datetime.now(timezone.utc))
        created_at = self._aware(user.created_at) if user.created_at else current
        starts_at = current if use_current_policy else min(created_at, current)
        expires_at = None
        if use_current_policy and policy.default_duration_days is not None:
            expires_at = starts_at + timedelta(days=policy.default_duration_days)
        grant = TaskQuotaGrant(
            user_id=user.id,
            amount=amount,
            starts_at=starts_at,
            expires_at=expires_at,
            label="注册基础额度",
            is_initial=True,
        )
        try:
            with self.session.begin_nested():
                self.session.add(grant)
                self.session.flush()
            return grant
        except IntegrityError:
            return self.session.scalar(
                select(TaskQuotaGrant).where(
                    TaskQuotaGrant.user_id == user.id,
                    TaskQuotaGrant.is_initial.is_(True),
                )
            )

    def grants_for(self, user_id: str) -> list[TaskQuotaGrant]:
        user = self.session.get(User, user_id)
        if user is None:
            raise NotFound("user not found")
        self.bootstrap_user(user)
        return list(
            self.session.scalars(
                select(TaskQuotaGrant)
                .where(TaskQuotaGrant.user_id == user_id)
                .order_by(TaskQuotaGrant.starts_at, TaskQuotaGrant.created_at)
            ).all()
        )

    def summary_for(self, user: User, at: datetime | None = None) -> dict:
        current = self._aware(at or datetime.now(timezone.utc))
        grants = self.grants_for(user.id) if user.role != "admin" else []
        grant_items = []
        for grant in grants:
            start = self._aware(grant.starts_at)
            end = self._aware(grant.expires_at) if grant.expires_at else None
            revoked = self._aware(grant.revoked_at) if grant.revoked_at else None
            if revoked is not None:
                status = "revoked"
            elif current < start:
                status = "future"
            elif end is not None and current >= end:
                status = "expired"
            else:
                status = "active"
            days_remaining = None
            if status == "active" and end is not None:
                days_remaining = max(1, ceil((end - current).total_seconds() / 86400))
                if end - current <= timedelta(days=7):
                    status = "expiring"
            grant_items.append(
                {
                    "grant": grant,
                    "status": status,
                    "days_remaining": days_remaining,
                }
            )
        return {
            "limit": self.limit_for(user, current),
            "active_usage": self.active_usage_for(user.id),
            "saved_usage": self.saved_usage_for(user.id),
            "max_saved_tasks": self.policy().max_saved_tasks,
            "grants": grant_items,
        }

    def limit_for(self, user: User, at: datetime | None = None) -> int | None:
        if user.role == "admin":
            return None
        current = at or datetime.now(timezone.utc)
        current = self._aware(current)
        self.bootstrap_user(user, effective_at=current)
        grants = self.session.scalars(
            select(TaskQuotaGrant).where(
                TaskQuotaGrant.user_id == user.id,
                TaskQuotaGrant.revoked_at.is_(None),
                TaskQuotaGrant.starts_at <= current,
                or_(TaskQuotaGrant.expires_at.is_(None), TaskQuotaGrant.expires_at > current),
            )
        ).all()
        return sum(grant.amount for grant in grants)

    def usage_for(self, user_id: str) -> int:
        return self.active_usage_for(user_id)

    def active_usage_for(self, user_id: str) -> int:
        return int(
            self.session.scalar(
                select(func.count(SparkTask.id)).where(
                    SparkTask.owner_user_id == user_id,
                    SparkTask.enabled.is_(True),
                )
            )
            or 0
        )

    def saved_usage_for(self, user_id: str) -> int:
        return int(
            self.session.scalar(
                select(func.count(SparkTask.id)).where(SparkTask.owner_user_id == user_id)
            )
            or 0
        )

    def assert_can_create(self, user: User) -> None:
        policy = self.policy()
        saved = self.saved_usage_for(user.id)
        if user.role != "admin" and saved >= policy.max_saved_tasks:
            raise ValidationError(f"普通用户最多保存 {policy.max_saved_tasks} 个任务")
        limit = self.limit_for(user)
        if limit is None:
            return
        usage = self.active_usage_for(user.id)
        if usage >= limit:
            raise ValidationError(
                f"当前启用任务 {usage}/{limit}，请暂停任务或联系管理员增加额度"
            )

    def assert_can_enable(self, user: User) -> None:
        limit = self.limit_for(user)
        if limit is None:
            return
        usage = self.active_usage_for(user.id)
        if usage >= limit:
            raise ValidationError(
                f"当前启用任务 {usage}/{limit}，请先暂停其他任务或联系管理员增加额度"
            )

    def update_policy(
        self,
        actor_id: str,
        default_amount: int,
        default_duration_days: int | None,
        max_saved_tasks: int,
    ) -> TaskQuotaPolicy:
        actor = self.session.get(User, actor_id)
        if actor is None or actor.role != "admin":
            raise NotFound("user not found")
        if not MIN_TASK_LIMIT <= default_amount <= MAX_TASK_LIMIT:
            raise ValidationError("默认任务额度须为 0–100")
        if default_duration_days is not None and not 1 <= default_duration_days <= 3650:
            raise ValidationError("默认有效期须为 1–3650 天")
        if not MIN_SAVED_TASKS <= max_saved_tasks <= MAX_SAVED_TASKS:
            raise ValidationError("任务保存上限须为 1–500")
        policy = self.policy()
        policy.default_amount = default_amount
        policy.default_duration_days = default_duration_days
        policy.max_saved_tasks = max_saved_tasks
        self.session.flush()
        self.audit.write(
            actor_id,
            "quota.policy_updated",
            "task_quota_policy",
            str(policy.id),
            detail=(
                f"default_amount={default_amount};duration_days={default_duration_days};"
                f"max_saved_tasks={max_saved_tasks}"
            ),
        )
        return policy

    def grant(
        self,
        actor_id: str,
        user_id: str,
        amount: int,
        starts_at: datetime,
        expires_at: datetime | None,
        label: str,
    ) -> TaskQuotaGrant:
        actor = self.session.get(User, actor_id)
        target = self.session.get(User, user_id)
        if actor is None or actor.role != "admin" or target is None:
            raise NotFound("user not found")
        if target.role == "admin":
            raise ValidationError("管理员账号无需设置任务额度")
        if not 1 <= amount <= MAX_TASK_LIMIT:
            raise ValidationError("单次增加额度须为 1–100")
        start = self._aware(starts_at)
        end = self._aware(expires_at) if expires_at is not None else None
        if end is not None and end <= start:
            raise ValidationError("额度到期时间必须晚于开始时间")
        clean_label = label.strip()
        if not clean_label or len(clean_label) > 64:
            raise ValidationError("额度名称须为 1–64 个字符")
        self.bootstrap_user(target)
        grant = TaskQuotaGrant(
            user_id=user_id,
            amount=amount,
            starts_at=start,
            expires_at=end,
            label=clean_label,
            created_by_user_id=actor_id,
        )
        self.session.add(grant)
        self.session.flush()
        self.audit.write(
            actor_id,
            "quota.granted",
            "task_quota_grant",
            grant.id,
            detail=f"user_id={user_id};amount={amount};expires_at={end.isoformat() if end else 'never'}",
        )
        return grant

    def update_grant(
        self,
        actor_id: str,
        grant_id: str,
        amount: int,
        starts_at: datetime,
        expires_at: datetime | None,
        label: str,
    ) -> TaskQuotaGrant:
        actor = self.session.get(User, actor_id)
        grant = self.session.get(TaskQuotaGrant, grant_id)
        if actor is None or actor.role != "admin" or grant is None:
            raise NotFound("quota grant not found")
        if grant.revoked_at is not None:
            raise ValidationError("已撤销的额度不能修改")
        if not 1 <= amount <= MAX_TASK_LIMIT:
            raise ValidationError("单条额度须为 1–100")
        start = self._aware(starts_at)
        end = self._aware(expires_at) if expires_at is not None else None
        if end is not None and end <= start:
            raise ValidationError("额度到期时间必须晚于开始时间")
        clean_label = label.strip()
        if not clean_label or len(clean_label) > 64:
            raise ValidationError("额度名称须为 1–64 个字符")
        grant.amount = amount
        grant.starts_at = start
        grant.expires_at = end
        grant.label = clean_label
        self.session.flush()
        self.audit.write(
            actor_id,
            "quota.updated",
            "task_quota_grant",
            grant.id,
            detail=f"amount={amount};starts_at={start.isoformat()};expires_at={end.isoformat() if end else 'never'}",
        )
        self.reconcile_user(grant.user_id)
        return grant

    def revoke(
        self, actor_id: str, grant_id: str, at: datetime | None = None
    ) -> list[str]:
        actor = self.session.get(User, actor_id)
        grant = self.session.get(TaskQuotaGrant, grant_id)
        if actor is None or actor.role != "admin" or grant is None:
            raise NotFound("quota grant not found")
        if grant.revoked_at is not None:
            raise ValidationError("该额度已经撤销")
        current = self._aware(at or datetime.now(timezone.utc))
        grant.revoked_at = current
        self.session.flush()
        self.audit.write(
            actor_id,
            "quota.revoked",
            "task_quota_grant",
            grant.id,
            detail=f"user_id={grant.user_id};revoked_at={current.isoformat()}",
        )
        return self.reconcile_user(grant.user_id, current)

    def reconcile_user(self, user_id: str, at: datetime | None = None) -> list[str]:
        user = self.session.get(User, user_id)
        if user is None:
            raise NotFound("user not found")
        limit = self.limit_for(user, at)
        if limit is None:
            return []
        enabled = list(
            self.session.scalars(
                select(SparkTask)
                .where(
                    SparkTask.owner_user_id == user_id,
                    SparkTask.enabled.is_(True),
                )
                .order_by(SparkTask.created_at, SparkTask.id)
            ).all()
        )
        excess = enabled[limit:]
        for task in excess:
            task.enabled = False
            task.next_run_at = None
            self.audit.write(
                None,
                "task.quota_auto_paused",
                "spark_task",
                task.id,
                detail=f"user_id={user_id};effective_limit={limit}",
            )
        self.session.flush()
        return [task.id for task in excess]

    def reconcile_all(self, at: datetime | None = None) -> list[str]:
        user_ids = self.session.scalars(
            select(SparkTask.owner_user_id)
            .join(User, User.id == SparkTask.owner_user_id)
            .where(
                SparkTask.enabled.is_(True),
                User.role != "admin",
            )
            .distinct()
        ).all()
        paused = []
        for user_id in user_ids:
            paused.extend(self.reconcile_user(user_id, at))
        return paused

    def set_limit(
        self, actor_id: str, user_id: str, limit: int
    ) -> TaskQuotaGrant:
        actor = self.session.get(User, actor_id)
        target = self.session.get(User, user_id)
        if actor is None or actor.role != "admin" or target is None:
            raise NotFound("user not found")
        if target.role == "admin":
            raise ValidationError("管理员账号无需设置任务上限")
        if limit < 1 or limit > MAX_TASK_LIMIT:
            raise ValidationError("任务上限须为 1–100")
        grants = self.grants_for(user_id)
        quota = grants[0]
        quota.amount = limit
        self.session.flush()
        self.audit.write(
            actor_id,
            "user.task_limit_updated",
            "user",
            user_id,
            detail=f"limit={limit};grant_id={quota.id}",
        )
        return quota

    @staticmethod
    def bucket_for(send_time: str) -> int:
        try:
            hour_text, minute_text = send_time.split(":", 1)
            hour, minute = int(hour_text), int(minute_text)
        except (AttributeError, TypeError, ValueError):
            raise ValidationError("发送时间格式必须为 HH:MM") from None
        if not (0 <= hour <= 23 and 0 <= minute <= 59):
            raise ValidationError("发送时间格式必须为 HH:MM")
        return hour * 60 + minute

    @staticmethod
    def time_for_bucket(bucket: int) -> str:
        minutes = bucket % MINUTES_PER_DAY
        return f"{minutes // 60:02d}:{minutes % 60:02d}"

    @staticmethod
    def _minutes_apart(left: int, right: int) -> int:
        distance = abs(left - right)
        return min(distance, MINUTES_PER_DAY - distance)

    def occupied_buckets(self, exclude_task_id: str | None = None) -> set[int]:
        query = select(SparkTask.send_time).where(SparkTask.enabled.is_(True))
        if exclude_task_id:
            query = query.where(SparkTask.id != exclude_task_id)
        return {
            self.bucket_for(send_time)
            for send_time in self.session.scalars(query).all()
        }

    def next_available_times(
        self,
        send_time: str,
        count: int = 3,
        exclude_task_id: str | None = None,
    ) -> tuple[str, ...]:
        origin = self.bucket_for(send_time)
        occupied = self.occupied_buckets(exclude_task_id)
        suggestions = []
        seen = {origin}
        for distance in range(1, MINUTES_PER_DAY):
            for candidate in (
                (origin - distance) % MINUTES_PER_DAY,
                (origin + distance) % MINUTES_PER_DAY,
            ):
                if candidate in seen:
                    continue
                seen.add(candidate)
                if any(
                    self._minutes_apart(candidate, value) < SLOT_MINUTES
                    for value in occupied
                ):
                    continue
                suggestions.append(self.time_for_bucket(candidate))
                if len(suggestions) == count:
                    return tuple(suggestions)
        return tuple(suggestions)

    def availability(
        self, send_time: str, exclude_task_id: str | None = None
    ) -> SlotAvailability:
        bucket = self.bucket_for(send_time)
        available = all(
            self._minutes_apart(bucket, occupied) >= SLOT_MINUTES
            for occupied in self.occupied_buckets(exclude_task_id)
        )
        return SlotAvailability(
            available=available,
            remaining=1 if available else 0,
            suggestions=()
            if available
            else self.next_available_times(send_time, 3, exclude_task_id),
        )

    def assert_slot_available(
        self, send_time: str, exclude_task_id: str | None = None
    ) -> None:
        if not self.availability(send_time, exclude_task_id).available:
            raise ValidationError("该时间不满足四分钟安全间隔，请选择推荐时间")

    def next_available_run_at(
        self, earliest: datetime, exclude_task_id: str | None = None
    ) -> datetime:
        candidate = (
            earliest.astimezone(timezone.utc)
            if earliest.tzinfo
            else earliest.replace(tzinfo=timezone.utc)
        )
        if candidate.second or candidate.microsecond:
            candidate = candidate.replace(second=0, microsecond=0) + timedelta(minutes=1)
        task_query = select(SparkTask.next_run_at).where(
            SparkTask.enabled.is_(True),
            SparkTask.next_run_at.is_not(None),
        )
        if exclude_task_id:
            task_query = task_query.where(SparkTask.id != exclude_task_id)
        occupied_values = list(self.session.scalars(task_query).all())
        occupied_values.extend(
            self.session.scalars(
                select(TaskRun.scheduled_for).where(
                    TaskRun.status == "running",
                    TaskRun.finished_at.is_(None),
                )
            ).all()
        )
        occupied = []
        for value in occupied_values:
            aware = value if value.tzinfo else value.replace(tzinfo=timezone.utc)
            occupied.append(aware.astimezone(timezone.utc))

        for _ in range(MINUTES_PER_DAY * 2):
            if all(
                abs((candidate - value).total_seconds()) >= SLOT_MINUTES * 60
                for value in occupied
            ):
                return candidate
            candidate += timedelta(minutes=1)
        raise ValidationError("未来两天内没有可用执行时段")

    def spread_enabled_schedule(self, at: datetime | None = None) -> list[str]:
        current = self._aware(at or datetime.now(timezone.utc))
        tasks = list(
            self.session.scalars(
                select(SparkTask)
                .where(SparkTask.enabled.is_(True))
                .order_by(SparkTask.send_time, SparkTask.created_at, SparkTask.id)
            ).all()
        )
        if not tasks:
            return []
        original_minutes = [self.bucket_for(task.send_time) for task in tasks]
        shifted_minutes = [original_minutes[0]]
        for minute in original_minutes[1:]:
            shifted_minutes.append(max(minute, shifted_minutes[-1] + SLOT_MINUTES))
        if shifted_minutes[-1] >= MINUTES_PER_DAY:
            raise ValidationError("任务顺延后超出当天范围，请先调整接近午夜的任务")
        if MINUTES_PER_DAY + shifted_minutes[0] - shifted_minutes[-1] < SLOT_MINUTES:
            raise ValidationError("午夜两侧任务不足四分钟安全间隔，请先手动调整")

        changed = []
        local_now = current.astimezone(SHANGHAI)
        for task, old_minute, new_minute in zip(tasks, original_minutes, shifted_minutes):
            if old_minute == new_minute:
                continue
            old_time = task.send_time
            new_time = self.time_for_bucket(new_minute)
            task.send_time = new_time
            candidate = local_now.replace(
                hour=new_minute // 60,
                minute=new_minute % 60,
                second=0,
                microsecond=0,
            )
            if candidate <= local_now:
                candidate += timedelta(days=1)
            task.next_run_at = candidate.astimezone(timezone.utc)
            changed.append(task.id)
            self.audit.write(
                None,
                "task.schedule_auto_shifted",
                "spark_task",
                task.id,
                detail=f"old_time={old_time};new_time={new_time};gap_minutes={SLOT_MINUTES}",
            )
        self.session.flush()
        return changed
