"""Integration tests require an explicitly supplied, disposable PostgreSQL DB."""
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
import os
from pathlib import Path
import threading
import uuid

import pytest
from sqlalchemy import create_engine, select, text
from sqlalchemy.engine import make_url
from sqlalchemy.orm import Session

from spark_console.browser_runtime import BrowserSlot
from spark_console.cluster import ClusterBrowserSlot, exclusive, NOTIFIER_LOCK
from spark_console.db import create_schema, session_scope
from spark_console.migrate_postgres import migrate
from spark_console.models import DouyinAccount, SparkTask, TaskQuotaGrant, User, WebSession
from spark_console.scheduler import claim_next_due_task


@pytest.fixture
def pg():
    url = os.environ.get('SPARK_TEST_POSTGRES_URL')
    if not url:
        pytest.skip('set SPARK_TEST_POSTGRES_URL to an isolated test database')
    root = create_engine(url)
    schema = 'spark_test_' + uuid.uuid4().hex
    with root.begin() as connection:
        connection.exec_driver_sql('CREATE SCHEMA ' + schema)
    scoped = make_url(url).update_query_dict({'options': '-csearch_path=' + schema})
    engine = create_engine(scoped)
    try:
        yield engine, scoped.render_as_string(hide_password=False)
    finally:
        engine.dispose()
        with root.begin() as connection:
            connection.exec_driver_sql('DROP SCHEMA ' + schema + ' CASCADE')
        root.dispose()


def seed(engine):
    with session_scope(engine) as db:
        user = User(username='shared', password_hash='hash', must_change_password=False)
        db.add(user)
        db.flush()
        account = DouyinAccount(owner_user_id=user.id, display_name='中文账号', encrypted_cookies=b'\x00\xff', cookie_nonce=b'n')
        db.add(account)
        db.flush()
        now = datetime(2026, 10, 14, 1, 0, tzinfo=timezone.utc)
        task = SparkTask(owner_user_id=user.id, douyin_account_id=account.id,
                         target_name='好友', send_time='09:00', message_template='火花',
                         enabled=True, next_run_at=now)
        db.add(task)
        db.flush()
        return user.id, now


def test_concurrent_nodes_only_claim_once(pg):
    engine, _ = pg
    create_schema(engine)
    _, now = seed(engine)
    barrier = threading.Barrier(2)
    def claim(node):
        barrier.wait(timeout=5)
        with session_scope(engine) as db:
            run = claim_next_due_task(db, now, node)
            return run.id if run else None
    with ThreadPoolExecutor(2) as pool:
        results = list(pool.map(claim, ['old', 'new']))
    assert sum(result is not None for result in results) == 1


def test_browser_owner_survives_other_node_start_and_release(pg, tmp_path):
    engine, _ = pg
    create_schema(engine)
    first = ClusterBrowserSlot(BrowserSlot(tmp_path/'one'), engine)
    second = ClusterBrowserSlot(BrowserSlot(tmp_path/'two'), engine)
    assert first.acquire()
    first.check()
    assert not second.acquire()
    first.release()
    assert second.acquire()
    second.release()


def test_lost_browser_connection_leaves_cooldown(pg, tmp_path):
    engine, _ = pg
    create_schema(engine)
    first = ClusterBrowserSlot(BrowserSlot(tmp_path/'one'), engine)
    second = ClusterBrowserSlot(BrowserSlot(tmp_path/'two'), engine)
    assert first.acquire()
    # Model a dead supervisor: close connection without declaring cleanup done.
    first._close()
    assert not second.acquire()
    with engine.begin() as connection:
        connection.exec_driver_sql("UPDATE cluster_browser_guard SET blocked_until=CURRENT_TIMESTAMP - INTERVAL '1 second'")
    assert second.acquire()
    second.release()


def test_notification_role_is_exclusive(pg):
    engine, _ = pg
    with exclusive(engine, NOTIFIER_LOCK) as one:
        assert one is not False
        with exclusive(engine, NOTIFIER_LOCK) as two:
            assert two is False
    with exclusive(engine, NOTIFIER_LOCK) as replacement:
        assert replacement is not False


def test_partial_indexes_allow_multiple_noninitial_grants(pg):
    engine, _ = pg
    create_schema(engine)
    user_id, now = seed(engine)
    with session_scope(engine) as db:
        for label in ['月卡一', '月卡二']:
            db.add(TaskQuotaGrant(user_id=user_id, amount=1, starts_at=now,
                                 label=label, is_initial=False))


def test_migration_preserves_binary_unicode_time_and_refuses_overwrite(pg, tmp_path):
    engine, url = pg
    snapshot = tmp_path/'snapshot.db'
    sqlite = create_engine('sqlite:///' + snapshot.as_posix())
    create_schema(sqlite)
    user_id, _ = seed(sqlite)
    sqlite.dispose()
    before = snapshot.read_bytes()
    with pytest.raises(ValueError, match='stop all SQLite writers'):
        migrate(snapshot, url)
    report = migrate(snapshot, url, writes_stopped=True)
    assert snapshot.read_bytes() == before
    assert report['users']['rows'] == 1
    with Session(engine) as db:
        account = db.scalar(select(DouyinAccount))
        assert account.encrypted_cookies == b'\x00\xff'
        assert account.display_name == '中文账号'
        assert db.get(User, user_id).must_change_password is False
    with pytest.raises(ValueError, match='destination must be empty'):
        migrate(snapshot, url, writes_stopped=True)


def test_migration_unknown_table_is_rejected_without_writes(pg, tmp_path):
    engine, url = pg
    snapshot = tmp_path/'unknown.db'
    sqlite = create_engine('sqlite:///' + snapshot.as_posix())
    with sqlite.begin() as connection:
        connection.exec_driver_sql('CREATE TABLE unknown_feature (id INTEGER PRIMARY KEY)')
    sqlite.dispose()
    with pytest.raises(ValueError, match='unmapped source tables'):
        migrate(snapshot, url, writes_stopped=True)
    with engine.connect() as connection:
        assert connection.scalar(text("SELECT count(*) FROM information_schema.tables WHERE table_schema=current_schema()")) == 0


def test_login_cookie_and_logout_work_across_two_app_nodes(pg, tmp_path):
    from fastapi.testclient import TestClient
    from spark_console.config import Settings
    from spark_console.security import PasswordService
    from spark_console.web.app import create_app
    engine, url = pg
    create_schema(engine)
    cookie, session = tmp_path/'cookie.key', tmp_path/'session.key'
    cookie.write_bytes(b'c' * 32)
    session.write_bytes(b's' * 32)
    settings = Settings(tmp_path, url, cookie, session)
    with session_scope(engine) as db:
        db.add(User(username='two_nodes', password_hash=PasswordService().hash('Test-only-123!'),
                    must_change_password=False))
    with TestClient(create_app(settings, engine), base_url='https://testserver') as old, \
         TestClient(create_app(settings, engine), base_url='https://testserver') as new:
        response = old.post('/login', data={'username': 'two_nodes', 'password': 'Test-only-123!'}, follow_redirects=False)
        assert response.status_code == 303
        new.cookies.update(old.cookies)
        assert new.get('/dashboard').status_code == 200
        with session_scope(engine) as db:
            token = db.scalar(select(WebSession.csrf_token))
        assert new.post('/logout', data={'csrf_token': token}, follow_redirects=False).status_code == 303
        assert old.get('/dashboard', follow_redirects=False).status_code == 303


def test_standby_blocks_writes_and_traffic_readiness(pg, tmp_path):
    from fastapi.testclient import TestClient
    from spark_console.config import Settings
    from spark_console.web.app import create_app
    engine, url = pg
    create_schema(engine)
    cookie, session = tmp_path/'cookie.key', tmp_path/'session.key'
    cookie.write_bytes(b'c' * 32)
    session.write_bytes(b's' * 32)
    settings = Settings(tmp_path, url, cookie, session, standby_mode=True)
    with TestClient(create_app(settings, engine), base_url='https://testserver') as client:
        assert client.get('/health/live').status_code == 200
        assert client.get('/health/ready').status_code == 503
        assert client.get('/login').status_code == 200
        assert client.post('/login', data={'username':'any','password':'any'}).status_code == 503
