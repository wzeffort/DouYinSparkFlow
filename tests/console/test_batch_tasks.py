from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from spark_console import models
from spark_console.crypto import CookieCipher
from spark_console.db import create_schema
from spark_console.services import ValidationError
from spark_console.services.accounts import AccountService
from spark_console.services.audits import AuditService
from spark_console.services.tasks import TaskService
from spark_console.services.task_capacity import TaskCapacityService


@pytest.fixture
def services():
    engine = create_engine('sqlite:///:memory:')
    create_schema(engine)
    with Session(engine) as db:
        owner = models.User(username='batch-owner', password_hash='unused')
        admin = models.User(username='batch-admin', password_hash='unused', role='admin')
        db.add_all([owner, admin])
        db.flush()
        audit = AuditService(db)
        accounts = AccountService(db, CookieCipher(b'c' * 32), audit)
        account = accounts.create(owner.id, 'fixture account', b'[{"name":"fixture","value":"test-only"}]')
        yield db, owner, admin, account, TaskService(db, accounts, audit), TaskCapacityService(db, audit)
    engine.dispose()


def recipients(count=5):
    return [dict(target_name=f'好友{i}', target_sec_uid='', message_template=f'专属内容{i}') for i in range(count)]


def create_batch(services, values=None, time='09:00'):
    _, owner, _, account, tasks, _ = services
    return tasks.create(owner.id, account.id, '', time, '', recipients=values if values is not None else recipients())


def test_batch_persists_five_distinct_messages(services):
    task = create_batch(services)
    values = services[4].recipients_for(task)
    assert [r['message_template'] for r in values] == [f'专属内容{i}' for i in range(5)]
    assert services[5].active_usage_for(services[1].id) == 1


@pytest.mark.parametrize('values', [[], recipients(6), [dict(target_name='A', message_template='')], recipients(1) * 2])
def test_invalid_batch_is_atomic(services, values):
    with pytest.raises(ValidationError):
        create_batch(services, values)
    assert services[0].scalar(select(models.SparkTask.id)) is None


def test_batch_rejects_foreign_identity(services):
    with pytest.raises(ValidationError):
        create_batch(services, [dict(target_name='name', target_sec_uid='not-owned', message_template='hello')])


def test_batch_overlap_rejected_and_pausing_releases_recipient(services):
    first = create_batch(services)
    with pytest.raises(ValidationError):
        create_batch(services, time='10:00')
    services[4].set_enabled_owned(services[1].id, first.id, False)
    second = create_batch(services, time='10:00')
    with pytest.raises(ValidationError):
        services[4].set_enabled_owned(services[1].id, first.id, True)
    assert second.enabled


def test_monthly_slots_expire_independently_and_renew_without_reset(services):
    db, owner, admin, _, tasks, capacity = services
    now = datetime.now(timezone.utc)
    initial = capacity.bootstrap_user(owner)
    initial.revoked_at = now
    grants = capacity.purchase_monthly(admin.id, owner.id, 2, at=now)
    assert len(grants) == 2
    assert all(g.amount == 1 and g.expires_at == now + timedelta(days=30) for g in grants)
    first = create_batch(services, recipients(1))
    second = create_batch(services, [dict(target_name='另一个', message_template='不同消息')], '10:00')
    binding = db.get(models.TaskQuotaBinding, first.id)
    first_grant = db.get(models.TaskQuotaGrant, binding.grant_id)
    first_grant.expires_at = now + timedelta(days=1)
    db.flush()
    capacity.reconcile_user(owner.id, now + timedelta(days=2))
    assert not first.enabled
    assert second.enabled
    renewed = capacity.renew_monthly(admin.id, first_grant.id, at=now + timedelta(days=2))
    assert renewed.expires_at == now + timedelta(days=32)
    assert capacity.task_authorized(first, now + timedelta(days=3))


def test_legacy_single_target_remains_readable(services):
    _, owner, _, account, tasks, _ = services
    task = tasks.create(owner.id, account.id, '旧好友', '09:00', '旧内容')
    assert tasks.recipients_for(task) == [dict(target_name='旧好友', target_sec_uid='', message_template='旧内容')]
