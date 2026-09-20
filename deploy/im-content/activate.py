"""Activate an already committed source archive. Root, explicit revision required.

Does not edit source, the existing compose, auth/notifier, or git working trees.
Builds only the two reviewed Dockerfiles and backs up the DB before additive DDL.
"""
import argparse
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
from datetime import datetime, timedelta, timezone

BASES = {
    'web':'sha256:2090b06e28dff3daf19cc466be8a0d97415d4cb2a712dcc85781dc19ef18a0b7',
    'worker':'sha256:5b71488121d9f92cbdfc572aff152f36bfae8b348edfeca50bb3f5a6823969c6',
}
PROJECT = Path('/opt/douyin-spark-console')
COMPOSE = PROJECT/'compose.console.yml'
ROOT = Path(__file__).resolve().parents[2]


def command(args, **kwargs):
    result = subprocess.run(args, capture_output=True, text=True, **kwargs)
    if result.returncode:
        # Never dump compose-resolved environment or other credentials on errors.
        raise RuntimeError(f'Command failed ({result.returncode}): {args[0]} {args[1]}')
    return result.stdout.strip()


def inspect(role, field):
    return command(['docker','inspect','--format',field,f'douyin-spark-console-spark-{role}-1'])


def quiet_database(data):
    with sqlite3.connect(f'file:{data}/spark.db?mode=ro',uri=True) as db:
        running = db.execute("select count(*) from task_runs where status='running' and finished_at is null").fetchone()[0]
        soon = (datetime.now(timezone.utc)+timedelta(minutes=5)).replace(tzinfo=None).isoformat(' ')
        due = db.execute('select count(*) from spark_tasks where enabled=1 and next_run_at<=?',(soon,)).fetchone()[0]
        if running or due:
            raise RuntimeError('Tasks active or due within five minutes; deployment not started')


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--revision',required=True)
    parser.add_argument('--verify-only',action='store_true')
    args=parser.parse_args()
    if not re.fullmatch('[0-9a-f]{40}',args.revision): raise ValueError('Full commit required')
    if ROOT != PROJECT/'releases'/('im-content-'+args.revision): raise ValueError('Unexpected release path')
    if not (ROOT/'COMMITTED_REVISION').is_file() or (ROOT/'COMMITTED_REVISION').read_text().strip()!=args.revision:
        raise ValueError('Archive revision receipt missing')
    env=dict(os.environ, SPARK_REVISION=args.revision, SPARK_RELEASE_ROOT=str(ROOT))
    compose=['docker','compose','-p','douyin-spark-console','-f',str(COMPOSE)]
    updated=compose+['-f',str(ROOT/'deploy/im-content/compose.release.yml')]
    before=json.loads(command(compose+['config','--format','json'],env=env))
    after=json.loads(command(updated+['config','--format','json'],env=env))
    for role in ('web','worker'):
        key='spark-'+role
        for field in ('image','build'):
            before['services'][key].pop(field,None);after['services'][key].pop(field,None)
    if before!=after: raise RuntimeError('Compose changed outside web/worker image/build')
    data=Path(inspect('worker','{{range .Mounts}}{{if eq .Destination "/data"}}{{.Source}}{{end}}{{end}}'))
    if not data.is_absolute() or not (data/'spark.db').is_file(): raise RuntimeError('Data volume not found')
    untouched={r:inspect(r,'{{.Id}}') for r in ('auth','notifier')}
    for role,expected in BASES.items():
        if inspect(role,'{{.Image}}')!=expected: raise RuntimeError('Live image changed: '+role)
        source_changes=command(['docker','diff',f'douyin-spark-console-spark-{role}-1'])
        if any(line.split(' ',1)[-1].startswith('/app') for line in source_changes.splitlines()):
            raise RuntimeError('Uncommitted container source changes: '+role)
        tag=f'spark-console-{role}:recipient-policy-20260917'
        if command(['docker','image','inspect','--format','{{.Id}}',tag])!=expected: raise RuntimeError('Build base moved')
        image=f'spark-console-{role}:im-content-{args.revision}'
        command(['docker','build','--network=none','--build-arg','REVISION='+args.revision,
                 '-f',str(ROOT/f'deploy/im-content/{role}.Dockerfile'),'-t',image,str(ROOT)])
        smoke="from sqlalchemy import create_engine; from spark_console.db import create_schema; from spark_console.models import RunMessageContent; from spark_console.message_content import render_content; from datetime import datetime; e=create_engine('sqlite://'); create_schema(e); assert render_content('fixture',datetime.now())[0]=='fixture'"
        smoke += '; from spark_console.web.app import create_app' if role=='web' else '; from core.im_evidence import receipt; from spark_console.executor import DouyinExecutor; assert receipt(b"bad") is None'
        command(['docker','run','--rm','--network','none','--read-only','--tmpfs','/tmp','--entrypoint','python',image,'-c',smoke])
        print(role+': candidate built and offline smoke passed',flush=True)
    if args.verify_only: return
    with (data/'browser-runtime.lock').open('a+b') as guard:
        fcntl.flock(guard,fcntl.LOCK_EX|fcntl.LOCK_NB)
        quiet_database(data)
        backup=PROJECT/'backups'/('im-content-'+args.revision)
        backup.mkdir(mode=0o700,exist_ok=False)
        shutil.copyfile(COMPOSE,backup/'compose.console.yml')
        os.chmod(backup/'compose.console.yml',0o600)
        with sqlite3.connect(f'file:{data}/spark.db?mode=ro',uri=True) as source, sqlite3.connect(backup/'spark.db') as dest:
            source.backup(dest)
            if dest.execute('pragma quick_check').fetchone()[0]!='ok': raise RuntimeError('Backup failed')
        os.chmod(backup/'spark.db',0o600)
        # Only the new table, no rewrite of existing schema or task schedules.
        ddl='from sqlalchemy import create_engine; from spark_console.models import RunMessageContent; RunMessageContent.__table__.create(create_engine("sqlite:////data/spark.db"),checkfirst=True)'
        command(['docker','run','--rm','--network','none','--read-only','--tmpfs','/tmp',
                 '--volumes-from','douyin-spark-console-spark-web-1','--entrypoint','python',
                 f'spark-console-web:im-content-{args.revision}','-c',ddl])
        try:
            command(updated+['up','-d','--no-deps','--no-build','spark-web','spark-worker'],env=env)
            healthy=False
            for _ in range(20):
                if inspect('web','{{.State.Health.Status}}')=='healthy':
                    healthy=True;break
                time.sleep(2)
            if not healthy: raise RuntimeError('Web readiness failed')
            for role in BASES:
                if inspect(role,'{{index .Config.Labels "org.opencontainers.image.revision"}}')!=args.revision:
                    raise RuntimeError('Revision mismatch')
                if inspect(role,'{{.State.Running}}')!='true': raise RuntimeError('Service not running')
            if any(inspect(r,'{{.Id}}')!=cid for r,cid in untouched.items()): raise RuntimeError('Untouched service changed')
        except Exception:
            command(compose+['up','-d','--no-deps','--no-build','spark-web','spark-worker'],env=env)
            raise RuntimeError('Activation failed; previous images restored, DB backup retained') from None
        print('Activated committed revision '+args.revision+'; auth/notifier unchanged; backup '+str(backup),flush=True)


if __name__=='__main__': main()
