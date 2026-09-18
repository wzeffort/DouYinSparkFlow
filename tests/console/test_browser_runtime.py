import importlib.util
import subprocess
import sys
from types import SimpleNamespace

import pytest


def runtime():
    assert importlib.util.find_spec('spark_console.browser_runtime'), 'shared admission is missing'
    from spark_console import browser_runtime
    return browser_runtime


def test_two_independent_owners_cannot_hold_browser_slot(tmp_path):
    module = runtime()
    first = module.BrowserSlot(tmp_path / 'browser.lock')
    second = module.BrowserSlot(tmp_path / 'browser.lock')
    assert first.acquire()
    try:
        assert not second.acquire()
    finally:
        first.release()
    assert second.acquire()
    second.release()


@pytest.mark.parametrize('memory,pressure,disk,expected', [
    ('MemAvailable: 700000 kB\n', 'full avg10=0.01 avg60=0.1 total=1\n', 2**31, None),
    ('MemAvailable: 192000 kB\n', 'full avg10=0.01 avg60=0.1 total=1\n', 2**31, 'low_memory'),
    ('MemAvailable: 700000 kB\n', 'full avg10=81.0 avg60=1 total=1\n', 2**31, 'memory_pressure'),
    ('MemAvailable: 700000 kB\n', 'full avg10=0.01 avg60=0.1 total=1\n', 1000, 'low_disk'),
    ('', '', 2**31, 'metrics_unavailable'),
])
def test_resource_gate_rejects_unsafe_or_missing_measurements(memory, pressure, disk, expected):
    assert runtime().assess_resources(memory, pressure, disk) == expected


def test_pressure_and_busy_slot_never_start_child(tmp_path):
    module = runtime()
    marker = tmp_path / 'spawned'
    command = [sys.executable, '-c', f'from pathlib import Path;Path({str(marker)!r}).touch()']
    slot = module.BrowserSlot(tmp_path/'browser.lock')
    assert module.admit_and_run(slot, command, 2, lambda:False, lambda:'low_memory') == 'low_memory'
    assert not marker.exists()
    other = module.BrowserSlot(tmp_path/'browser.lock')
    assert other.acquire()
    try:
        assert module.admit_and_run(slot, command, 2, lambda:False, lambda:None) == 'browser_busy'
        assert not marker.exists()
    finally:
        other.release()


def test_pending_job_lookup_does_not_launch_or_claim(services):
    module = runtime()
    from tests.console.test_batch_tasks import create_batch
    from datetime import datetime, timedelta, timezone
    from spark_console.models import TaskRun
    from sqlalchemy import select
    db = services[0]
    now = datetime.now(timezone.utc)
    assert not module.has_pending(db, 'auth', now)
    task = create_batch(services)
    task.next_run_at = now - timedelta(seconds=1)
    db.flush()
    assert module.has_pending(db, 'worker', now)
    assert not db.scalars(select(TaskRun)).all()


from tests.console.test_batch_tasks import services


def test_failed_cleanup_quarantines_slot_until_operator_intervention(tmp_path, monkeypatch):
    module = runtime()
    slot = module.BrowserSlot(tmp_path/'browser.lock')
    def failed(*args, **kwargs):
        raise module.CleanupFailed('fixture unkillable process')
    monkeypatch.setattr(module, 'run_job_process', failed)
    try:
        with pytest.raises(module.CleanupFailed):
            module.admit_and_run(slot, ['unused'], 1, lambda:False, lambda:None)
        assert slot.quarantined
        assert not module.BrowserSlot(tmp_path/'browser.lock').acquire()
    finally:
        slot.quarantined = False  # test-only stand-in for operator/container teardown
        slot.release()
