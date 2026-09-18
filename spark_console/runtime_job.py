"""One disposable browser job. Launched only by browser_runtime supervisor."""
import asyncio
from datetime import datetime, timezone
import os
import sys


async def run_once(role, boot_time):
    from spark_console.config import Settings
    from spark_console.db import create_engine_for
    settings = Settings.from_env(os.environ)
    engine = create_engine_for(settings)
    try:
        if role == 'worker':
            from spark_console.worker import Worker
            worker = Worker(settings, engine, started_at=boot_time,
                            recovery_at=datetime.now(timezone.utc),
                            clock_offset_seconds=float(os.environ.get('SPARK_CLOCK_OFFSET_SECONDS','0')))
            await worker.run_once()
        elif role == 'auth':
            from spark_console.auth_worker import AuthWorker
            worker = AuthWorker(settings, engine)
            try:
                await worker.run_once()
            finally:
                await worker.close()
        else:
            raise ValueError('unknown role')
    finally:
        engine.dispose()


if __name__ == '__main__':
    if len(sys.argv) != 3:
        raise SystemExit(2)
    asyncio.run(run_once(sys.argv[1], datetime.fromisoformat(sys.argv[2])))
