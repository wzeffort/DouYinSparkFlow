"""Deploy only the committed account/profile fix over verified live images.

No source edits, database migration, task creation, or original compose changes.
"""
import argparse
from datetime import datetime, timedelta, timezone
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import sqlite3
import subprocess
import time

PROJECT = Path('/opt/douyin-spark-console')
ROOT = Path(__file__).resolve().parents[2]
PREVIOUS = '8bde0e97ce7a1b0182ce56af31e7cbbd6b8534a3'
BASES = {
    'web': ('spark-console-web:im-content-' + PREVIOUS, 'sha256:257d2f1e7b38d69c28d4b504226323383dff429ef97b4bf25d6f148b0e9ab186'),
    'worker': ('spark-console-worker:im-content-' + PREVIOUS, 'sha256:d0890b0879c08b03ce0d07bb20ca13fd9ed64654bc9b567c69ad089381a6384a'),
    'auth': ('douyin-spark-console-spark-auth', 'sha256:a2619453d77d8396c327b8e0a7a3453bbe44c49ce09acb159a33fc17a80c168d'),
}
FILES = {
    'web': ['spark_console/services/accounts.py', 'spark_console/message_content.py',
            'spark_console/static/batch_tasks.js', 'spark_console/templates/accounts.html',
            'spark_console/templates/tasks.html', 'spark_console/templates/task_edit.html'],
    'worker': ['spark_console/message_content.py'],
    'auth': ['spark_console/auth_scanner.py', 'spark_console/services/accounts.py'],
}


def command(args, **kwargs):
    result = subprocess.run(args, capture_output=True, text=True, timeout=180, **kwargs)
    if result.returncode:
        raise RuntimeError(f'Command failed: {args[0]} {args[1]} (exit {result.returncode}); raw output suppressed')
    return result.stdout.strip()


def container(role):
    return 'douyin-spark-console-spark-' + role + '-1'


def inspect(role, field):
    return command(['docker', 'inspect', '--format', field, container(role)])


def quiet_database(data):
    now = datetime.now(timezone.utc).replace(tzinfo=None)
    with sqlite3.connect(f'file:{data}/spark.db?mode=ro', uri=True) as db:
        counts = [
            db.execute("select count(*) from task_runs where status='running' and finished_at is null").fetchone()[0],
            db.execute('select count(*) from spark_tasks where enabled=1 and next_run_at<=?', ((now+timedelta(minutes=5)).isoformat(' '),)).fetchone()[0],
            db.execute('select count(*) from douyin_login_sessions where finished_at is null and expires_at>?', (now.isoformat(' '),)).fetchone()[0],
        ]
    if any(counts):
        raise RuntimeError('Active/due tasks or login sessions; deployment postponed')


def ready():
    for _ in range(25):
        if (inspect('web', '{{.State.Health.Status}}') == 'healthy'
                and all(inspect(role, '{{.State.Running}}') == 'true' for role in BASES)):
            return
        time.sleep(2)
    raise RuntimeError('Service readiness failed')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--revision', required=True)
    parser.add_argument('--verify-only', action='store_true')
    args = parser.parse_args()
    if not re.fullmatch('[0-9a-f]{40}', args.revision):
        raise ValueError('Full Git commit required')
    if ROOT != PROJECT/'releases'/('account-profile-' + args.revision):
        raise ValueError('Unexpected release path')
    env = dict(os.environ, SPARK_REVISION=args.revision)
    base = ['docker', 'compose', '-p', 'douyin-spark-console', '-f', str(PROJECT/'compose.console.yml')]
    updated = base + ['-f', str(ROOT/'deploy/account-profile/compose.release.yml')]
    rollback = base + ['-f', str(ROOT/'deploy/account-profile/compose.rollback.yml')]
    before = json.loads(command(rollback + ['config', '--format', 'json'], env=env))
    after = json.loads(command(updated + ['config', '--format', 'json'], env=env))
    for role in BASES:
        before['services']['spark-'+role].pop('image', None)
        after['services']['spark-'+role].pop('image', None)
    if before != after:
        raise RuntimeError('Configuration changed outside selected images')
    data = Path(inspect('worker', '{{range .Mounts}}{{if eq .Destination "/data"}}{{.Source}}{{end}}{{end}}'))
    if not data.is_absolute() or not (data/'spark.db').is_file():
        raise RuntimeError('Data volume unavailable')
    notifier = inspect('notifier', '{{.Id}}')
    quiet_database(data)
    for role, (tag, expected) in BASES.items():
        if inspect(role, '{{.Image}}') != expected or command(['docker', 'image', 'inspect', '--format', '{{.Id}}', tag]) != expected:
            raise RuntimeError('Baseline image changed: '+role)
        changes = command(['docker', 'diff', container(role)])
        if any(line.split(' ', 1)[-1].startswith('/app') for line in changes.splitlines()):
            raise RuntimeError('Container source changed: '+role)
        mounts = json.loads(inspect(role, '{{json .Mounts}}'))
        if any(m['Destination'] == '/app' or m['Destination'].startswith('/app/') for m in mounts):
            raise RuntimeError('Source mount would override image')
        image = f'spark-console-{role}:account-profile-{args.revision}'
        command(['docker', 'build', '--network=none', '--build-arg', 'REVISION='+args.revision,
                 '-f', str(ROOT/f'deploy/account-profile/{role}.Dockerfile'), '-t', image, str(ROOT)])
        smoke = 'from spark_console.services.accounts import AccountService' if role != 'worker' else 'from spark_console.message_content import fetch_quote'
        if role == 'web': smoke += '; from spark_console.web.app import create_app'
        if role == 'auth': smoke += '; from spark_console.auth_scanner import DouyinQrScanner; assert callable(DouyinQrScanner._account_profile)'
        command(['docker', 'run', '--rm', '--network', 'none', '--read-only', '--tmpfs', '/tmp', '--entrypoint', 'python', image, '-c', smoke])
        print(role + ': offline image check passed', flush=True)
    if args.verify_only:
        return
    with (data/'browser-runtime.lock').open('a+b') as guard:
        fcntl.flock(guard, fcntl.LOCK_EX | fcntl.LOCK_NB)
        quiet_database(data)
        backup = PROJECT/'backups'/('account-profile-'+args.revision)
        backup.mkdir(mode=0o700, exist_ok=False)
        shutil.copyfile(PROJECT/'compose.console.yml', backup/'compose.console.yml')
        os.chmod(backup/'compose.console.yml', 0o600)
        with sqlite3.connect(f'file:{data}/spark.db?mode=ro', uri=True) as source, sqlite3.connect(backup/'spark.db') as dest:
            source.backup(dest)
            if dest.execute('pragma quick_check').fetchone()[0] != 'ok':
                raise RuntimeError('Backup integrity failed')
        os.chmod(backup/'spark.db', 0o600)
        services = ['spark-'+role for role in BASES]
        try:
            command(updated+['up', '-d', '--no-deps', '--no-build', *services], env=env)
            ready()
            for role, paths in FILES.items():
                if inspect(role, '{{index .Config.Labels "org.opencontainers.image.revision"}}') != args.revision:
                    raise RuntimeError('Wrong revision: '+role)
                for path in paths:
                    actual = command(['docker', 'exec', container(role), 'sha256sum', '/app/'+path]).split()[0]
                    if actual != hashlib.sha256((ROOT/path).read_bytes()).hexdigest():
                        raise RuntimeError('Wrong deployed file: '+path)
            if inspect('notifier', '{{.Id}}') != notifier:
                raise RuntimeError('Notifier unexpectedly changed')
        except Exception:
            command(rollback+['up', '-d', '--no-deps', '--no-build', *services], env=env)
            ready()
            if any(inspect(role, '{{.Image}}') != expected for role, (_, expected) in BASES.items()):
                raise RuntimeError('Rollback image verification failed') from None
            raise RuntimeError('Deployment failed; previous images restored and verified') from None
        print('DEPLOYED '+args.revision+'; source hashes verified; backup='+str(backup), flush=True)


if __name__ == '__main__':
    main()
