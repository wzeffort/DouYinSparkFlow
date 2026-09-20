"""Deploy a committed web-only display fix, preserving the running workers."""
import argparse
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import runpy
import shutil
import sqlite3

ROOT = Path(__file__).resolve().parents[2]
PROJECT = Path('/opt/douyin-spark-console')
PREVIOUS = '774099bbe517d090840fcbac98efd5e768da2a9c'
BASE_TAG = 'spark-console-web:account-profile-' + PREVIOUS
BASE_ID = 'sha256:87da050b314ac1266c3cba95c51a177c70a54318248d52146357943c8e9e2084'
SOURCE = 'spark_console/services/accounts.py'
helpers = runpy.run_path(str(ROOT/'deploy/account-profile/activate.py'))
command, inspect, ready = (helpers[key] for key in ('command', 'inspect', 'ready'))
quiet_database = helpers['quiet_database']


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--revision', required=True)
    parser.add_argument('--verify-only', action='store_true')
    args = parser.parse_args()
    if not re.fullmatch('[0-9a-f]{40}', args.revision):
        raise ValueError('Full Git revision required')
    if ROOT != PROJECT/'releases'/('hide-account-id-'+args.revision):
        raise ValueError('Unexpected release directory')
    if inspect('web', '{{.Image}}') != BASE_ID:
        raise RuntimeError('Live web image changed')
    if command(['docker', 'image', 'inspect', '--format', '{{.Id}}', BASE_TAG]) != BASE_ID:
        raise RuntimeError('Base tag changed')
    diff = command(['docker', 'diff', 'douyin-spark-console-spark-web-1'])
    if any(line.split(' ', 1)[-1].startswith('/app') for line in diff.splitlines()):
        raise RuntimeError('Unexpected container source changes')
    mounts = json.loads(inspect('web', '{{json .Mounts}}'))
    if any(m['Destination'] == '/app' or m['Destination'].startswith('/app/') for m in mounts):
        raise RuntimeError('Source mount would override the release')
    preserved = {role: inspect(role, '{{.Id}}') for role in ('worker', 'auth', 'notifier')}
    data = Path(inspect('web', '{{range .Mounts}}{{if eq .Destination "/data"}}{{.Source}}{{end}}{{end}}'))
    if not data.is_absolute() or not (data/'spark.db').is_file():
        raise RuntimeError('Data volume unavailable')
    quiet_database(data)
    previous_compose = PROJECT/'releases'/('account-profile-'+PREVIOUS)/'deploy/account-profile/compose.release.yml'
    env = dict(os.environ, SPARK_REVISION=PREVIOUS, HIDE_ID_REVISION=args.revision)
    rollback = ['docker', 'compose', '-p', 'douyin-spark-console', '-f', str(PROJECT/'compose.console.yml'), '-f', str(previous_compose)]
    updated = rollback + ['-f', str(ROOT/'deploy/hide-account-id/compose.release.yml')]
    before = json.loads(command(rollback+['config', '--format', 'json'], env=env))
    after = json.loads(command(updated+['config', '--format', 'json'], env=env))
    before['services']['spark-web'].pop('image')
    after['services']['spark-web'].pop('image')
    if before != after:
        raise RuntimeError('Configuration changed outside the web image')
    image = 'spark-console-web:hide-account-id-'+args.revision
    command(['docker', 'build', '--network=none', '--build-arg', 'REVISION='+args.revision,
             '-f', str(ROOT/'deploy/hide-account-id/web.Dockerfile'), '-t', image, str(ROOT)])
    smoke = 'from spark_console.web.app import create_app; from spark_console.services.accounts import AccountService; import inspect; assert "item.id[:8]" not in inspect.getsource(AccountService.list_owned)'
    command(['docker', 'run', '--rm', '--network', 'none', '--read-only', '--tmpfs', '/tmp', '--entrypoint', 'python', image, '-c', smoke])
    print('Web-only candidate built; offline smoke passed', flush=True)
    if args.verify_only:
        return
    with (data/'browser-runtime.lock').open('a+b') as guard:
        fcntl.flock(guard, fcntl.LOCK_EX | fcntl.LOCK_NB)
        quiet_database(data)
        backup = PROJECT/'backups'/('hide-account-id-'+args.revision)
        backup.mkdir(mode=0o700, exist_ok=False)
        shutil.copyfile(PROJECT/'compose.console.yml', backup/'compose.console.yml')
        os.chmod(backup/'compose.console.yml', 0o600)
        with sqlite3.connect(f'file:{data}/spark.db?mode=ro', uri=True) as source, sqlite3.connect(backup/'spark.db') as dest:
            source.backup(dest)
            if dest.execute('pragma quick_check').fetchone()[0] != 'ok':
                raise RuntimeError('Backup integrity failed')
        os.chmod(backup/'spark.db', 0o600)
        try:
            command(updated+['up', '-d', '--no-deps', '--no-build', 'spark-web'], env=env)
            ready()
            if inspect('web', '{{index .Config.Labels "org.opencontainers.image.revision"}}') != args.revision:
                raise RuntimeError('Wrong live revision')
            actual = command(['docker', 'exec', 'douyin-spark-console-spark-web-1', 'sha256sum', '/app/'+SOURCE]).split()[0]
            if actual != hashlib.sha256((ROOT/SOURCE).read_bytes()).hexdigest():
                raise RuntimeError('Deployed file mismatch')
            if any(inspect(role, '{{.Id}}') != cid for role, cid in preserved.items()):
                raise RuntimeError('Untouched container changed')
        except Exception:
            command(rollback+['up', '-d', '--no-deps', '--no-build', 'spark-web'], env=env)
            ready()
            if inspect('web', '{{.Image}}') != BASE_ID:
                raise RuntimeError('Rollback verification failed') from None
            raise RuntimeError('Activation failed; previous web image restored') from None
        print('DEPLOYED '+args.revision+'; worker/auth/notifier unchanged; backup='+str(backup), flush=True)


if __name__ == '__main__':
    main()
