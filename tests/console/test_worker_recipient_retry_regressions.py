import asyncio
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

import pytest
from sqlalchemy import select

from spark_console import models
from spark_console.config import Settings
from spark_console.executor import ExecutionResult
from spark_console.services import ValidationError
from spark_console.worker import Worker
from tests.console.test_batch_tasks import services, create_batch, recipients


def make_worker(db, tmp_path, now, executor=None):
    settings = Settings(data_dir=tmp_path, database_url='sqlite:///:memory:',
                        cookie_key_file=tmp_path/'cookie.key', session_key_file=tmp_path/'session.key')
    settings.cookie_key_file.write_bytes(b'c' * 32)
    return Worker(settings, db.bind, executor=executor, started_at=now)


@pytest.mark.parametrize('long_spacing,minutes,code', [(True, 25, 'retry_scheduled_25m'), (False, 1, 'retry_scheduled_1m')])
def test_retry_code_matches_actual_delay(services, tmp_path, long_spacing, minutes, code):
    db = services[0]
    task = create_batch(services)
    now = datetime.now(timezone.utc)
    run = models.TaskRun(task_id=task.id, scheduled_for=now, started_at=now, status='running')
    worker = make_worker(db, tmp_path, now)
    db.add(run)
    db.flush()
    with patch('spark_console.worker.TaskCapacityService.next_available_run_at', side_effect=lambda at, task_id: at):
        result = worker._schedule_retry(db, task, run, now, 'batch_retry', long_spacing=long_spacing)
    assert result.error_code == code
    assert task.next_run_at == now + timedelta(minutes=minutes)


def failed_history(db, task, now):
    for days in (2, 1, 0):
        at = now - timedelta(days=days)
        run = models.TaskRun(task_id=task.id, scheduled_for=at, started_at=at, status='failed')
        db.add(run)
        db.flush()
        db.add(models.TaskRunRecipient(run_id=run.id, position=0, account_id=task.douyin_account_id,
            target_name='好友0', target_sec_uid='', message_template='fixture',
            status='failed', stage='selecting_target'))
    db.flush()


def test_auto_remove_first_still_executes_remaining_batch(services, tmp_path):
    db = services[0]
    task = create_batch(services, recipients(3))
    now = datetime.now(timezone.utc)
    observed = []
    class Executor:
        async def execute(self, *args, **kwargs):
            pytest.fail('must not use the stale single-recipient target')
        async def execute_batch(self, cookies, pending, *, before_send, on_result, credential_version):
            observed.extend(r['position'] for r in pending)
            for index in range(len(pending)):
                assert await before_send(index)
                await on_result(index, ExecutionResult(True, 'complete'))
    worker = make_worker(db, tmp_path, now, Executor())
    failed_history(db, task, now)
    worker._pause_stuck_selection_recipients(db, task, now)
    assert db.get(models.SparkTaskRecipient, (task.id, 0)) is None
    task.next_run_at = now + timedelta(seconds=1)
    db.commit()
    result = asyncio.run(worker.run_once(now + timedelta(seconds=1)))
    assert result.status == 'success'
    assert observed == [1, 2]


def test_last_failed_recipient_pauses_task_without_legacy_fallback(services, tmp_path):
    db = services[0]
    task = create_batch(services, recipients(1))
    now = datetime.now(timezone.utc)
    worker = make_worker(db, tmp_path, now)
    failed_history(db, task, now)
    worker._pause_stuck_selection_recipients(db, task, now)
    assert not task.enabled and task.next_run_at is None
    assert db.get(models.SparkTaskRecipient, (task.id, 0)) is not None


def test_edit_sparse_batch_requires_complete_list(services):
    db, owner, _, account, tasks, _ = services
    task = create_batch(services, recipients(3))
    db.delete(db.get(models.SparkTaskRecipient, (task.id, 0)))
    db.flush()
    with pytest.raises(ValidationError, match='完整好友列表'):
        tasks.update_owned(owner.id, task.id, account.id, 'old target', '09:00', 'old message')


def test_enabling_sparse_batch_checks_overlap(services):
    db, owner, _, _, tasks, _ = services
    task = create_batch(services, recipients(2))
    db.delete(db.get(models.SparkTaskRecipient, (task.id, 0)))
    db.flush()
    tasks.set_enabled_owned(owner.id, task.id, False)
    create_batch(services, recipients(2)[1:], time='10:00')
    with pytest.raises(ValidationError, match='已在'):
        tasks.set_enabled_owned(owner.id, task.id, True)


@pytest.mark.parametrize('auth_failure', [False, True])
def test_batch_success_refreshes_account_but_later_auth_failure_wins(services, tmp_path, auth_failure):
    db, _, _, account, _, _ = services
    task = create_batch(services)
    now = datetime.now(timezone.utc)
    account.validation_state = 'unknown'
    account.last_verified_at = None
    task.next_run_at = now
    db.commit()
    class Executor:
        async def execute_batch(self, cookies, pending, *, before_send, on_result, credential_version):
            for index in range(len(pending)):
                assert await before_send(index)
                result = ExecutionResult(False, 'authenticating', 'login_expired') if auth_failure and index == 1 else ExecutionResult(True, 'complete')
                await on_result(index, result)
                if not result.success: break
    worker = make_worker(db, tmp_path, now, Executor())
    asyncio.run(worker.run_once(now))
    db.expire_all()
    checked = db.get(models.DouyinAccount, account.id)
    assert checked.validation_state == ('invalid' if auth_failure else 'valid')
    assert checked.last_verified_at is not None
    if not auth_failure:
        assert checked.invalidated_at is None
        assert checked.invalid_reason_code is None
        assert checked.auth_incident_id is None
