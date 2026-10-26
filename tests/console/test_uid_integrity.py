"""Regression coverage for identity enrichment and recipient failure history."""
from datetime import datetime, timedelta, timezone
from contextlib import contextmanager
from unittest.mock import patch
import pytest

from spark_console import models
from spark_console.worker import Worker
from spark_console.services.batch_execution import BatchRunService
from spark_console.services.manual_retries import ManualRetryService
from tests.console.test_batch_tasks import services, create_batch, recipients


def bare_worker(db):
    worker = object.__new__(Worker)
    worker.engine = db.bind
    return worker


def test_uid_enrichment_refuses_conflicting_nickname_and_remark(services):
    db, _, _, account, _, _ = services
    task = create_batch(services, [dict(target_name='SharedName', target_sec_uid='', message_template='test')])
    task_id, account_id = task.id, account.id
    db.add_all([
        models.DouyinContactIdentity(account_id=account_id, sec_uid='person-A', nickname='OriginalA', remark_name='SharedName'),
        models.DouyinContactIdentity(account_id=account_id, sec_uid='person-B', nickname='SharedName', remark_name='OtherB'),
    ])
    db.commit()
    pending = [dict(position=0, target_name='SharedName', target_sec_uid='')]
    bare_worker(db)._enrich_recipient_sec_uids(pending, task_id, account_id)
    assert pending[0]['target_sec_uid'] == ''
    db.expire_all()
    assert db.get(models.SparkTaskRecipient, (task_id, 0)).target_sec_uid == ''


def test_uid_enrichment_keeps_manual_retry_snapshot_consistent(services):
    db, _, _, account, _, _ = services
    task = create_batch(services, recipients(1))
    task_id, account_id = task.id, account.id
    account.validation_state = 'valid'
    now = datetime.now(timezone.utc)
    task.next_run_at = now + timedelta(days=1)
    db.add(models.DouyinContactIdentity(account_id=account_id, sec_uid='person-A', nickname='好友0'))
    run = models.TaskRun(task_id=task_id, scheduled_for=now, started_at=now, status='running')
    db.add(run)
    db.flush()
    run_id = run.id
    pending = BatchRunService(db).prepare(run, task)
    db.commit()
    bare_worker(db)._enrich_recipient_sec_uids(pending, task_id, account_id, run_id=run_id)
    db.expire_all()
    run = db.get(models.TaskRun, run_id)
    run.status, run.stage, run.error_code, run.finished_at = 'failed', 'batch_complete', 'target_not_found', now
    row = db.get(models.TaskRunRecipient, (run_id, 0))
    row.status, row.stage, row.error_code = 'failed', 'selecting_target', 'target_not_found'
    db.flush()
    assert db.get(models.SparkTaskRecipient, (task_id, 0)).target_sec_uid == 'person-A'
    assert row.target_sec_uid == 'person-A'
    entry = ManualRetryService(db, now=now+timedelta(seconds=1))._entry(run)
    assert entry['eligible'] is True
    assert entry['reason'] == ''


@pytest.mark.parametrize('old_uid,new_uid,old_name,new_name', [
    ('person-A', 'person-B', 'OldFriend', 'NewFriend'),
    ('', '', 'OldFriend', 'NewFriend'),
])
def test_old_recipient_failures_do_not_remove_replacement(services, old_uid, new_uid, old_name, new_name):
    db, _, _, account, _, _ = services
    task = create_batch(services, recipients(2))
    now = datetime.now(timezone.utc)
    current = db.get(models.SparkTaskRecipient, (task.id, 0))
    current.target_name, current.target_sec_uid = new_name, new_uid
    for days in (2, 1, 0):
        at = now - timedelta(days=days)
        run = models.TaskRun(task_id=task.id, scheduled_for=at, started_at=at, status='failed', finished_at=at)
        db.add(run)
        db.flush()
        db.add(models.TaskRunRecipient(run_id=run.id, position=0, account_id=account.id,
            target_name=old_name if days else new_name, target_sec_uid=old_uid if days else new_uid,
            message_template='test', status='failed', stage='selecting_target'))
    db.flush()
    bare_worker(db)._pause_stuck_selection_recipients(db, task, now)
    db.flush()
    assert db.get(models.SparkTaskRecipient, (task.id, 0)) is not None


def test_same_uid_still_counts_across_nickname_changes(services):
    db, _, _, account, _, _ = services
    task = create_batch(services, recipients(2))
    now = datetime.now(timezone.utc)
    current = db.get(models.SparkTaskRecipient, (task.id, 0))
    current.target_name, current.target_sec_uid = 'NewName', 'same-person'
    for days in (2, 1, 0):
        at = now - timedelta(days=days)
        run = models.TaskRun(task_id=task.id, scheduled_for=at, status='failed', finished_at=at)
        db.add(run)
        db.flush()
        db.add(models.TaskRunRecipient(run_id=run.id, position=0, account_id=account.id,
            target_name='OldName' if days else 'NewName', target_sec_uid='same-person',
            message_template='test', status='failed', stage='selecting_target'))
    db.flush()
    bare_worker(db)._pause_stuck_selection_recipients(db, task, now)
    db.flush()
    assert db.get(models.SparkTaskRecipient, (task.id, 0)) is None


def test_failed_enrichment_transaction_does_not_change_in_memory_identity(services):
    db, _, _, account, _, _ = services
    task = create_batch(services, recipients(1))
    task_id, account_id = task.id, account.id
    db.add(models.DouyinContactIdentity(account_id=account_id, sec_uid='person-A', nickname='好友0'))
    db.commit()
    pending = [dict(position=0, target_name='好友0', target_sec_uid='')]
    @contextmanager
    def failing_transaction(engine):
        from sqlalchemy.orm import Session
        with Session(engine) as session:
            yield session
            session.rollback()
            raise RuntimeError('synthetic commit failure')
    with patch('spark_console.worker.session_scope', failing_transaction):
        bare_worker(db)._enrich_recipient_sec_uids(pending, task_id, account_id)
    assert pending[0]['target_sec_uid'] == ''
    db.expire_all()
    assert db.get(models.SparkTaskRecipient, (task_id, 0)).target_sec_uid == ''
