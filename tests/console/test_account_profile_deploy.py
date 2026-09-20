from datetime import datetime, timedelta, timezone
from pathlib import Path
import runpy
import sqlite3
from types import SimpleNamespace
from unittest.mock import patch

import pytest


def deploy_module():
    # Only the preflight is executed here; Linux locking is exercised on host.
    with patch.dict('sys.modules', {'fcntl': SimpleNamespace()}):
        return runpy.run_path(str(Path(__file__).resolve().parents[2] / 'deploy/account-profile/activate.py'))


@pytest.mark.parametrize('busy', [None, 'running', 'due', 'login'])
def test_deploy_refuses_active_work(tmp_path, busy):
    now = datetime.now(timezone.utc).replace(tzinfo=None)
    with sqlite3.connect(tmp_path/'spark.db') as db:
        db.executescript('''
            create table task_runs(status text, finished_at text);
            create table spark_tasks(enabled integer, next_run_at text);
            create table douyin_login_sessions(finished_at text, expires_at text);
        ''')
        if busy == 'running':
            db.execute("insert into task_runs values ('running', null)")
        if busy == 'due':
            db.execute('insert into spark_tasks values (1, ?)', ((now+timedelta(minutes=2)).isoformat(' '),))
        if busy == 'login':
            db.execute('insert into douyin_login_sessions values (null, ?)', ((now+timedelta(minutes=2)).isoformat(' '),))
    check = deploy_module()['quiet_database']
    if busy:
        with pytest.raises(RuntimeError, match='postponed'):
            check(tmp_path)
    else:
        check(tmp_path)


def test_release_scope_matches_repaired_files():
    module = deploy_module()
    assert set(module['BASES']) == {'web', 'worker', 'auth'}
    assert module['FILES']['worker'] == ['spark_console/message_content.py']
    assert 'spark_console/auth_scanner.py' in module['FILES']['auth']
    assert 'spark_console/services/accounts.py' in module['FILES']['web']
    assert len(module['PREVIOUS']) == 40
