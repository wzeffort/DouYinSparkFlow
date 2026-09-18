"""Single-host browser admission and bounded process-tree ownership.

Only supervisors launch production browser jobs. The shared local-volume lock
is held until the entire child process group is gone, not until a lease expires.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
import json
import math
import os
from pathlib import Path
import re
import shutil
import signal
import subprocess
import sys
import time


class BrowserSlot:
    def __init__(self, path):
        self.path = Path(path)
        self.file = None
        self.quarantined = False

    def acquire(self):
        if self.file is not None:
            raise RuntimeError('slot already held')
        file = self.path.open('a+b')
        try:
            if os.name == 'nt':
                import msvcrt
                if file.seek(0, 2) == 0:
                    file.write(b'0')
                    file.flush()
                file.seek(0)
                msvcrt.locking(file.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            file.close()
            return False
        self.file = file
        return True

    def release(self):
        if self.file is None:
            return
        if self.quarantined:
            raise RuntimeError('cleanup incomplete: admission stays closed')
        if os.name == 'nt':
            import msvcrt
            self.file.seek(0)
            msvcrt.locking(self.file.fileno(), msvcrt.LK_UNLCK, 1)
        self.file.close()
        self.file = None


def assess_resources(memory, pressure, free_disk):
    try:
        available = int(re.search(r'^MemAvailable:\s+(\d+) kB$', memory, re.M)[1]) * 1024
        full = float(re.search(r'^full avg10=([\d.]+)', pressure, re.M)[1])
        if not math.isfinite(full) or not 0 <= full <= 100:
            return 'metrics_unavailable'
    except (TypeError, ValueError):
        return 'metrics_unavailable'
    if available < 512 * 1024**2:
        return 'low_memory'
    if full >= 5:
        return 'memory_pressure'
    if free_disk < 1024**3:
        return 'low_disk'
    return None


def resource_reason(data_dir):
    try:
        return assess_resources(Path('/proc/meminfo').read_text(),
                                Path('/proc/pressure/memory').read_text(),
                                shutil.disk_usage(data_dir).free)
    except OSError:
        return 'metrics_unavailable'


class CleanupFailed(RuntimeError):
    """Keep the slot closed until an operator/container teardown removes jobs."""


def _signal_group(pgid, signum):
    try:
        os.killpg(pgid, signum)
    except ProcessLookupError:
        pass


def _owned_descendants():
    records = {}
    for entry in Path('/proc').iterdir():
        if not entry.name.isdigit():
            continue
        try:
            fields = (entry/'stat').read_text().rsplit(')', 1)[1].split()
            records[int(entry.name)] = (int(fields[1]), fields[19])
        except (OSError, ValueError, IndexError):
            continue
    owned = {os.getpid()}
    while True:
        children = {pid for pid, (parent, _) in records.items() if parent in owned}
        expanded = owned | children
        if expanded == owned:
            break
        owned = expanded
    return {pid: records[pid] for pid in owned if pid != os.getpid()}


def _signal_owned(signum):
    for pid, identity in _owned_descendants().items():
        # Recheck parent ownership and start time, not just a reusable numeric PID.
        if _owned_descendants().get(pid) != identity:
            continue
        try:
            os.kill(pid, signum)
        except ProcessLookupError:
            pass


def _group_alive(process):
    process.poll()
    # The supervisor is a subreaper. Reap only this job's adopted descendants.
    if process.returncode is not None:
        try:
            while os.waitpid(-1, os.WNOHANG)[0]:
                pass
        except ChildProcessError:
            pass
    if _owned_descendants():
        return True
    try:
        os.killpg(process.pid, 0)
        return True
    except ProcessLookupError:
        return False


def cleanup_group(process):
    if process.pid == os.getpgrp():
        raise CleanupFailed('refusing supervisor group')
    for signum, duration in ((signal.SIGTERM, 2), (signal.SIGKILL, 3)):
        _signal_group(process.pid, signum)
        _signal_owned(signum)
        deadline = time.monotonic() + duration
        while _group_alive(process) and time.monotonic() < deadline:
            _signal_owned(signum)
            time.sleep(0.05)
        if not _group_alive(process):
            process.wait()
            return
    raise CleanupFailed('job process group still exists')


def run_job_process(command, timeout, stopping, heartbeat=lambda: None, inherited_fd=None):
    if sys.platform != 'linux':
        raise RuntimeError('production browser supervisor requires Linux')
    import ctypes
    if ctypes.CDLL(None, use_errno=True).prctl(36, 1, 0, 0, 0) != 0:
        raise RuntimeError('cannot enable child subreaper')
    if _owned_descendants():
        raise CleanupFailed('supervisor has surviving children before admission')
    process = subprocess.Popen(command, start_new_session=True,
                               pass_fds=() if inherited_fd is None else (inherited_fd,),
                               stdin=subprocess.DEVNULL,
                               stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    started = time.monotonic()
    next_heartbeat = started
    outcome = 'completed'
    try:
        while process.poll() is None:
            if stopping():
                outcome = 'cancelled'
                break
            if time.monotonic() - started >= timeout:
                outcome = 'job_timeout'
                break
            if time.monotonic() >= next_heartbeat:
                heartbeat()
                next_heartbeat = time.monotonic() + 10
            time.sleep(0.1)
        if process.returncode not in (None, 0):
            outcome = 'job_failed'
        return outcome
    finally:
        cleanup_group(process)


def admit_and_run(slot, command, timeout, stopping, probe, heartbeat=lambda: None,
                  admitted=lambda: None, reconcile=lambda: None):
    if not slot.acquire():
        return 'browser_busy'
    try:
        reconcile()
        reason = probe()
        if reason:
            return reason
        if stopping():
            return 'cancelled'
        admitted()
        return run_job_process(command, timeout, stopping, heartbeat, slot.file.fileno())
    except CleanupFailed:
        slot.quarantined = True
        raise
    finally:
        if not slot.quarantined:
            slot.release()


def has_pending(db, role, now):
    from sqlalchemy import select, or_
    from spark_console.models import ContactSyncState, DouyinLoginSession, SparkTask, TaskRun, ScanStatus
    if role == 'auth':
        # Also give the child a chance to reconcile abandoned scan state.
        if db.scalar(select(DouyinLoginSession.id).where(
            DouyinLoginSession.slot == 'global').limit(1)) is not None:
            return True
        sync = db.scalar(select(ContactSyncState).where(ContactSyncState.status.in_(('queued','running'))).limit(1))
        if sync is None:
            return False
        # Allow reconciliation even while a task is due; it opens no browser.
        requested = sync.requested_at.replace(tzinfo=timezone.utc) if sync.requested_at.tzinfo is None else sync.requested_at
        if sync.status == 'running' or requested <= now-timedelta(minutes=15):
            return True
        return (db.scalar(select(TaskRun.id).where(TaskRun.status=='running').limit(1)) is None
                and db.scalar(select(SparkTask.id).where(SparkTask.enabled.is_(True),
                    SparkTask.douyin_account_id.is_not(None), SparkTask.next_run_at <= now+timedelta(seconds=100)).limit(1)) is None)
    if role != 'worker':
        raise ValueError('unknown browser role')
    if db.scalar(select(TaskRun.id).where(TaskRun.status == 'running').limit(1)):
        return True
    return db.scalar(select(SparkTask.id).where(
        SparkTask.enabled.is_(True), SparkTask.douyin_account_id.is_not(None),
        SparkTask.next_run_at <= now).limit(1)) is not None


def run_supervisor(role):
    from spark_console.config import Settings
    from spark_console.db import create_engine_for, create_schema, session_scope
    from spark_console.models import WorkerLock
    if role not in {'worker', 'auth'} or sys.platform != 'linux':
        raise RuntimeError('unsupported supervisor deployment')
    settings = Settings.from_env(os.environ)
    engine = create_engine_for(settings)
    create_schema(engine)
    singleton = BrowserSlot(settings.data_dir / (role + '-supervisor.lock'))
    if not singleton.acquire():
        raise RuntimeError('supervisor already running')
    slot = BrowserSlot(settings.data_dir / 'browser-runtime.lock')
    stopping = False
    def stop(*_):
        nonlocal stopping
        stopping = True
    for signum in (signal.SIGINT, signal.SIGTERM):
        signal.signal(signum, stop)
    boot_time = datetime.now(timezone.utc)
    last_state = None
    def reconcile():
        if role == 'auth':
            from spark_console.services.contacts import ContactSyncService
            with session_scope(engine) as db:
                ContactSyncService(db).recover()
    def heartbeat():
        if role == 'worker':
            with session_scope(engine) as db:
                row = db.get(WorkerLock, 1)
                if row is None:
                    row = WorkerLock(id=1)
                    db.add(row)
                row.worker_id = f'supervisor-{os.getpid()}'
                row.lease_until = datetime.now(timezone.utc) + timedelta(seconds=40)
    def status(state):
        nonlocal last_state
        payload = {'role': role, 'state': state, 'updated_at': datetime.now(timezone.utc).isoformat()}
        temporary = settings.data_dir / (role + '-runtime.json.tmp')
        temporary.write_text(json.dumps(payload), encoding='utf-8')
        temporary.replace(settings.data_dir / (role + '-runtime.json'))
        if state != last_state:
            print(json.dumps(payload), flush=True)
            last_state = state
    try:
        while not stopping:
            try:
                heartbeat()
                now = datetime.now(timezone.utc)
                with session_scope(engine) as db:
                    pending = has_pending(db, role, now)
                if pending:
                    command = [sys.executable, '-m', 'spark_console.runtime_job', role,
                               boot_time.isoformat()]
                    status('waiting')
                    outcome = admit_and_run(slot, command, 210 if role == 'worker' else 330,
                                            lambda: stopping, lambda: resource_reason(settings.data_dir),
                                            lambda: (heartbeat(), status('running')),
                                            lambda: status('running'), reconcile=reconcile)
                    status(outcome)
                else:
                    status('idle')
            except CleanupFailed:
                status('cleanup_failed')
                # Fail closed: keep the global lock, never overlap a surviving job.
                while not stopping:
                    heartbeat()
                    time.sleep(1)
                break
            except Exception:
                # No exception strings: dependency errors may contain credentials.
                status('runtime_error')
            end = time.monotonic() + (5 if role == 'worker' else 2)
            while not stopping and time.monotonic() < end:
                time.sleep(0.2)
    finally:
        singleton.release()
        engine.dispose()
