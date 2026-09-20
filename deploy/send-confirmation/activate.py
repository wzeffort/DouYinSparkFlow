"""Deploy committed send confirmation only; never create or replay a task."""
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
PROFILE = '774099bbe517d090840fcbac98efd5e768da2a9c'
DISPLAY = '2fb4271b71b0f05cbaa60f83544fb48f72391ab4'
BASES = {
    'web': ('spark-console-web:hide-account-id-'+DISPLAY, 'sha256:c78387ec36b9749c6470504df15f066fd623e89428e42b30b3ced8b6c4ca0e54'),
    'worker': ('spark-console-worker:account-profile-'+PROFILE, 'sha256:3d2af4c7615110340d632f1ed129686a32d2e5083fbd775f2ab5493017a4e497'),
}
FILES = {'web': ['spark_console/web/app.py', 'spark_console/templates/runs.html'],
         'worker': ['core/page_send_evidence.py', 'spark_console/executor.py']}
helpers = runpy.run_path(str(ROOT/'deploy/account-profile/activate.py'))
command, inspect, ready, quiet_database = (helpers[key] for key in ('command', 'inspect', 'ready', 'quiet_database'))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--revision', required=True)
    parser.add_argument('--verify-only', action='store_true')
    args = parser.parse_args()
    if not re.fullmatch('[0-9a-f]{40}', args.revision): raise ValueError('Full revision required')
    if ROOT != PROJECT/'releases'/('send-confirmation-'+args.revision): raise ValueError('Wrong release path')
    env = dict(os.environ, SPARK_REVISION=PROFILE, HIDE_ID_REVISION=DISPLAY, SEND_CONFIRM_REVISION=args.revision)
    rollback = ['docker', 'compose', '-p', 'douyin-spark-console', '-f', str(PROJECT/'compose.console.yml'),
                '-f', str(PROJECT/'releases'/('account-profile-'+PROFILE)/'deploy/account-profile/compose.release.yml'),
                '-f', str(PROJECT/'releases'/('hide-account-id-'+DISPLAY)/'deploy/hide-account-id/compose.release.yml')]
    updated = rollback + ['-f', str(ROOT/'deploy/send-confirmation/compose.release.yml')]
    before = json.loads(command(rollback+['config', '--format', 'json'], env=env))
    after = json.loads(command(updated+['config', '--format', 'json'], env=env))
    for role in BASES:
        before['services']['spark-'+role].pop('image'); after['services']['spark-'+role].pop('image')
    if before != after: raise RuntimeError('Unexpected configuration change')
    data = Path(inspect('worker', '{{range .Mounts}}{{if eq .Destination "/data"}}{{.Source}}{{end}}{{end}}'))
    if not data.is_absolute() or not (data/'spark.db').is_file(): raise RuntimeError('Data volume missing')
    quiet_database(data)
    preserved = {role: inspect(role, '{{.Id}}') for role in ('auth', 'notifier')}
    for role, (tag, expected) in BASES.items():
        if inspect(role, '{{.Image}}') != expected or command(['docker','image','inspect','--format','{{.Id}}',tag]) != expected:
            raise RuntimeError('Baseline changed: '+role)
        diff = command(['docker','diff','douyin-spark-console-spark-'+role+'-1'])
        if any(line.split(' ',1)[-1].startswith('/app') for line in diff.splitlines()): raise RuntimeError('Container source changed')
        if any(m['Destination']=='/app' or m['Destination'].startswith('/app/') for m in json.loads(inspect(role,'{{json .Mounts}}'))):
            raise RuntimeError('Source mount overrides image')
        image = f'spark-console-{role}:send-confirmation-{args.revision}'
        command(['docker','build','--network=none','--build-arg','REVISION='+args.revision,'-f',str(ROOT/f'deploy/send-confirmation/{role}.Dockerfile'),'-t',image,str(ROOT)])
        smoke = 'from spark_console.web.app import create_app' if role=='web' else 'from spark_console.executor import DouyinExecutor; from core.page_send_evidence import PageSendEvidence; assert callable(DouyinExecutor._send_and_confirm)'
        command(['docker','run','--rm','--network','none','--read-only','--tmpfs','/tmp','--entrypoint','python',image,'-c',smoke])
        print(role+': offline smoke passed',flush=True)
    if args.verify_only: return
    with (data/'browser-runtime.lock').open('a+b') as guard:
        fcntl.flock(guard,fcntl.LOCK_EX|fcntl.LOCK_NB)
        quiet_database(data)
        backup=PROJECT/'backups'/('send-confirmation-'+args.revision)
        backup.mkdir(mode=0o700,exist_ok=False)
        shutil.copyfile(PROJECT/'compose.console.yml',backup/'compose.console.yml'); os.chmod(backup/'compose.console.yml',0o600)
        with sqlite3.connect(f'file:{data}/spark.db?mode=ro',uri=True) as source, sqlite3.connect(backup/'spark.db') as dest:
            source.backup(dest)
            if dest.execute('pragma quick_check').fetchone()[0]!='ok': raise RuntimeError('Backup integrity failed')
        os.chmod(backup/'spark.db',0o600)
        services=['spark-'+role for role in BASES]
        try:
            command(updated+['up','-d','--no-deps','--no-build',*services],env=env)
            ready()
            for role, paths in FILES.items():
                if inspect(role,'{{index .Config.Labels "org.opencontainers.image.revision"}}')!=args.revision: raise RuntimeError('Wrong deployed revision')
                for path in paths:
                    actual=command(['docker','exec','douyin-spark-console-spark-'+role+'-1','sha256sum','/app/'+path]).split()[0]
                    if actual!=hashlib.sha256((ROOT/path).read_bytes()).hexdigest(): raise RuntimeError('Wrong source hash')
            if any(inspect(role,'{{.Id}}')!=cid for role,cid in preserved.items()): raise RuntimeError('Untouched container changed')
        except Exception:
            command(rollback+['up','-d','--no-deps','--no-build',*services],env=env)
            ready()
            if any(inspect(role,'{{.Image}}')!=expected for role,(_,expected) in BASES.items()): raise RuntimeError('Rollback verification failed') from None
            raise RuntimeError('Deployment failed; prior images restored and verified') from None
        print('DEPLOYED '+args.revision+'; auth/notifier unchanged; backup='+str(backup),flush=True)


if __name__=='__main__': main()
