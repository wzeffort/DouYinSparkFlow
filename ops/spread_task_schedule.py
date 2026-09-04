from __future__ import annotations

import argparse
import os

from sqlalchemy.orm import Session

from spark_console.config import Settings
from spark_console.db import create_engine_for
from spark_console.models import SparkTask
from spark_console.services.audits import AuditService
from spark_console.services.task_capacity import TaskCapacityService


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Preview or apply the global four-minute task spacing migration."
    )
    parser.add_argument(
        "--apply",
        action="store_true",
        help="Commit the proposed task time changes; otherwise roll them back.",
    )
    args = parser.parse_args()

    settings = Settings.from_env(os.environ)
    engine = create_engine_for(settings)
    session = Session(engine, expire_on_commit=False)
    try:
        before = {
            task.id: task.send_time
            for task in session.query(SparkTask).filter(SparkTask.enabled.is_(True))
        }
        changed_ids = TaskCapacityService(
            session, AuditService(session)
        ).spread_enabled_schedule()
        changes = [
            (task_id, before[task_id], session.get(SparkTask, task_id).send_time)
            for task_id in changed_ids
        ]
        print(f"mode={'apply' if args.apply else 'preview'} changed={len(changes)}")
        for task_id, old_time, new_time in changes:
            print(f"{task_id}\t{old_time}\t{new_time}")
        if args.apply:
            session.commit()
        else:
            session.rollback()
        return 0
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


if __name__ == "__main__":
    raise SystemExit(main())
