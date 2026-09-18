from datetime import datetime, timedelta, timezone
import asyncio

from sqlalchemy import select

from spark_console import models
from spark_console.executor import ExecutionResult
from tests.console.test_batch_tasks import services, create_batch


def test_checkpoint_blocks_expired_quota_and_recovery_never_replays_sending(services):
    from spark_console.services.batch_execution import BatchRunService
    db, owner, admin, _, tasks, capacity = services
    now = datetime.now(timezone.utc)
    capacity.bootstrap_user(owner).revoked_at = now
    grant = capacity.purchase_monthly(admin.id, owner.id, 1, at=now)[0]
    task = create_batch(services)
    run = models.TaskRun(task_id=task.id, scheduled_for=now, status='running')
    db.add(run)
    db.flush()
    batch = BatchRunService(db)
    prepared = batch.prepare(run, task)
    assert len(prepared) == 5
    assert batch.before_send(run.id, 0, now)
    batch.record(run.id, 0, ExecutionResult(True, 'complete'), now)
    assert batch.before_send(run.id, 1, now)
    batch.interrupt(run.id, now)
    source = db.get(models.TaskRunRecipient, (run.id, 1))
    assert source.status == 'uncertain' and not source.retryable
    db.add(models.TaskBatchRetry(task_id=task.id, source_run_id=run.id, scheduled_for=now + timedelta(minutes=4)))
    retry = models.TaskRun(task_id=task.id, scheduled_for=now + timedelta(minutes=4), status='running')
    db.add(retry)
    db.flush()
    pending = batch.prepare(retry, task)
    assert [item['position'] for item in pending] == [2, 3, 4]
    grant.expires_at = now
    db.flush()
    assert not batch.before_send(retry.id, 2, now)
    assert db.get(models.TaskRunRecipient, (retry.id, 0)).status == 'success'


def test_partial_result_preserves_distinct_snapshot_after_task_change(services):
    from spark_console.services.batch_execution import BatchRunService
    db, _, _, _, _, _ = services
    task = create_batch(services)
    now = datetime.now(timezone.utc)
    run = models.TaskRun(task_id=task.id, scheduled_for=now, status='running')
    db.add(run)
    db.flush()
    batch = BatchRunService(db)
    batch.prepare(run, task)
    batch.record(run.id, 0, ExecutionResult(True, 'submitted'), now)
    batch.record(run.id, 1, ExecutionResult(False, 'selecting_target', 'not_found', 'missing'), now)
    task.message_template = 'changed'
    rows = db.scalars(select(models.TaskRunRecipient).where(models.TaskRunRecipient.run_id == run.id).order_by(models.TaskRunRecipient.position)).all()
    assert [r.message_template for r in rows] == [f'专属内容{i}' for i in range(5)]
    assert rows[0].status == 'submitted'


def test_midnight_retry_keeps_original_checkpoints(services):
    from spark_console.services.batch_execution import BatchRunService
    db = services[0]
    task = create_batch(services)
    now = datetime(2026, 9, 4, 15, 59, tzinfo=timezone.utc)
    run = models.TaskRun(task_id=task.id, scheduled_for=now, status='failed')
    db.add(run)
    db.flush()
    batch = BatchRunService(db)
    batch.prepare(run, task)
    batch.record(run.id, 0, ExecutionResult(True, 'submitted'), now)
    batch.record(run.id, 1, ExecutionResult(False, 'sending', 'delivery_uncertain'), now)
    batch.interrupt(run.id, now)
    retry_time = now + timedelta(minutes=4)
    link = models.TaskBatchRetry(task_id=task.id, source_run_id=run.id)
    link.scheduled_for = retry_time
    db.add(link)
    retry = models.TaskRun(task_id=task.id, scheduled_for=retry_time, status='running')
    db.add(retry)
    db.flush()
    assert [r['position'] for r in batch.prepare(retry, task)] == [2, 3, 4]


def test_worker_batch_commits_each_checkpoint_and_reuses_one_executor_call(services, tmp_path):
    from spark_console.config import Settings
    from spark_console.db import session_scope
    from spark_console.worker import Worker
    db, _, _, _, _, _ = services
    task = create_batch(services)
    now = datetime.now(timezone.utc)
    task.next_run_at = now
    task_id = task.id
    db.commit()
    settings = Settings(data_dir=tmp_path, database_url='sqlite:///:memory:',
                        cookie_key_file=tmp_path/'cookie.key', session_key_file=tmp_path/'session.key')
    settings.cookie_key_file.write_bytes(b'c' * 32)
    class FakeExecutor:
        calls = 0
        async def execute_batch(self, cookies, recipients, *, before_send, on_result, credential_version):
            self.calls += 1
            self.buffer = cookies
            assert [r['message_template'] for r in recipients] == [f'专属内容{i}' for i in range(5)]
            for position in range(5):
                assert await before_send(position)
                with session_scope(db.bind) as check:
                    row = check.scalar(select(models.TaskRunRecipient).where(models.TaskRunRecipient.position == position))
                    assert row.status == 'sending'
                await on_result(position, ExecutionResult(True, 'complete'))
            return []
    executor = FakeExecutor()
    worker = Worker(settings, db.bind, executor=executor, started_at=now)
    run = asyncio.run(worker.run_once(now))
    assert run.status == 'success'
    assert executor.calls == 1 and len(executor.buffer) == 0
    db.expire_all()
    assert db.get(models.SparkTask, task_id).enabled
    assert len(db.scalars(select(models.TaskRunRecipient).where(models.TaskRunRecipient.status == 'success')).all()) == 5


def test_batch_missing_friend_enqueues_existing_notification(services, tmp_path):
    from spark_console.config import Settings
    from spark_console.pii import PiiCipher
    from spark_console.worker import Worker
    db, owner, _, _, _, _ = services
    task = create_batch(services)
    now = datetime.now(timezone.utc)
    task.next_run_at = now
    owner.email_ciphertext, owner.email_nonce = PiiCipher(b'p'*32).encrypt_email('fixture@example.com', aad=f'user:{owner.id}'.encode())
    owner.email_verified_at = now
    db.add(models.NotificationPreference(user_id=owner.id, task_repeated_failure_email=True))
    db.commit()
    settings = Settings(data_dir=tmp_path, database_url='sqlite:///:memory:', cookie_key_file=tmp_path/'cookie.key',
                        session_key_file=tmp_path/'session.key', pii_key_file=tmp_path/'pii.key', email_enabled=True)
    settings.cookie_key_file.write_bytes(b'c'*32)
    settings.pii_key_file.write_bytes(b'p'*32)
    class Executor:
        async def execute_batch(self, cookies, recipients, *, before_send, on_result, credential_version):
            for position in range(5):
                await on_result(position, ExecutionResult(False, 'selecting_target', 'target_not_found', '未找到好友'))
    worker = Worker(settings, db.bind, executor=Executor(), started_at=now)
    result = asyncio.run(worker.run_once(now))
    assert result.status == 'failed'
    db.expire_all()
    events = db.scalars(select(models.NotificationEvent)).all()
    assert len(events) == 1 and events[0].kind == 'task_target_not_found'
