from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from sqlalchemy import select
from sqlalchemy.orm import Session

from spark_console.models import SparkTask, TaskRun, TaskRunRecipient, WorkerLock


SHANGHAI = ZoneInfo("Asia/Shanghai")


@dataclass(frozen=True)
class PlatformStatus:
    total: int
    success: int
    confirmed: int
    submitted: int
    running: int
    pending: int
    failed: int
    worker_online: bool
    updated_at: datetime


def _aware_utc(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    return value.astimezone(timezone.utc) if value.tzinfo else value.replace(tzinfo=timezone.utc)


def build_platform_status(
    session: Session, now: datetime | None = None
) -> PlatformStatus:
    current = _aware_utc(now or datetime.now(timezone.utc))
    local_now = current.astimezone(SHANGHAI)
    local_start = local_now.replace(hour=0, minute=0, second=0, microsecond=0)
    start = local_start.astimezone(timezone.utc)
    end = (local_start + timedelta(days=1)).astimezone(timezone.utc)
    task_ids = set(
        session.scalars(
            select(SparkTask.id).where(SparkTask.enabled.is_(True))
        ).all()
    )
    latest_by_task = {}
    runs = session.scalars(
        select(TaskRun)
        .where(
            TaskRun.scheduled_for >= start,
            TaskRun.scheduled_for < end,
        )
        .order_by(TaskRun.scheduled_for.desc(), TaskRun.id.desc())
    ).all()
    # Keep today's outcomes visible even when a task is subsequently paused.
    # Retries remain history entries; the overview counts each task once.
    for run in runs:
        task_ids.add(run.task_id)
        latest_by_task.setdefault(run.task_id, run)

    latest_ids = [run.id for run in latest_by_task.values()]
    unconfirmed_ids = set(session.scalars(select(TaskRunRecipient.run_id).where(
        TaskRunRecipient.run_id.in_(latest_ids),
        TaskRunRecipient.status.in_(['submitted', 'uncertain']),
    )).all()) if latest_ids else set()
    success = confirmed = submitted = running = failed = 0
    for run in latest_by_task.values():
        if run.status == "success":
            success += 1
            if run.stage == 'submitted' or run.error_code == 'delivery_confirmation_unavailable' or run.id in unconfirmed_ids:
                submitted += 1
            else:
                confirmed += 1
        elif run.status == "running":
            running += 1
        elif run.status in {"failed", "skipped", "partial"}:
            failed += 1
    pending = len(task_ids) - success - running - failed
    lock = session.get(WorkerLock, 1)
    lease_until = _aware_utc(lock.lease_until) if lock else None
    return PlatformStatus(
        total=len(task_ids),
        success=success,
        confirmed=confirmed,
        submitted=submitted,
        running=running,
        pending=max(0, pending),
        failed=failed,
        worker_online=bool(lease_until and lease_until > current),
        updated_at=current,
    )
