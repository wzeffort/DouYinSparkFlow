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
    delivered_total: int = 0
    stability_rate: int = 0
    service_days: int = 0


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
    delivered_total = int(
        session.scalar(
            select(func.count(TaskRunRecipient.id)).where(
                TaskRunRecipient.status.in_(("success", "submitted"))
            )
        )
        or 0
    )
    # Final-delivery stability: per (task, day, recipient), a day counts as
    # delivered when any attempt that day ended delivered - retries and
    # manual re-sends heal earlier failures instead of counting against it.
    day_rows = session.execute(
        select(
            TaskRun.task_id,
            TaskRun.scheduled_for,
            TaskRunRecipient.position,
            TaskRunRecipient.status,
        ).join(TaskRun, TaskRunRecipient.run_id == TaskRun.id)
    ).all()
    outcomes: dict[tuple, set] = {}
    for task_id, scheduled_for, position, status in day_rows:
        day = scheduled_for.astimezone(SHANGHAI).date()
        outcomes.setdefault((task_id, day, position), set()).add(status)
    attempted_groups = len(outcomes)
    delivered_groups = sum(
        1
        for statuses in outcomes.values()
        if statuses & {"success", "submitted"}
    )
    stability_rate = (
        round(delivered_groups / attempted_groups * 100)
        if attempted_groups
        else 0
    )
    delivered_days = {
        key[1] for key, statuses in outcomes.items() if statuses & {"success", "submitted"}
    }
    service_days = 0
    if delivered_days:
        today = datetime.now(timezone.utc).astimezone(SHANGHAI).date()
        service_days = (today - min(delivered_days)).days + 1
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
        delivered_total=delivered_total,
        stability_rate=stability_rate,
        service_days=service_days,
    )
