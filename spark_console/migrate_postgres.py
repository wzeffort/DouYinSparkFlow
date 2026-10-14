"""Offline migration from an immutable SQLite snapshot to an empty PostgreSQL DB."""
import argparse
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import sqlite3

from sqlalchemy import Boolean, DateTime, MetaData, Table, create_engine, func, inspect, select, text
from spark_console.db import create_schema
from spark_console.models import Base
from spark_console import referral_models, push_models, profile_models


def normalize(row, table):
    result = dict(row)
    for name, value in result.items():
        column = table.c[name]
        if value is None:
            continue
        if isinstance(column.type, Boolean):
            if value not in (0, 1, False, True):
                raise ValueError(f'invalid boolean: {table.name}.{name}')
            result[name] = bool(value)
        elif isinstance(column.type, DateTime):
            value = datetime.fromisoformat(value) if isinstance(value, str) else value
            result[name] = value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value.astimezone(timezone.utc)
    return result


def digest(rows):
    def encode(value):
        if isinstance(value, bytes):
            return {'bytes': value.hex()}
        if isinstance(value, datetime):
            return value.astimezone(timezone.utc).isoformat()
        raise TypeError(type(value).__name__)
    checksum = hashlib.sha256()
    for row in rows:
        checksum.update(json.dumps(dict(row), sort_keys=True, ensure_ascii=False, default=encode).encode())
        checksum.update(b'\n')
    return checksum.hexdigest()


def migrate(snapshot, destination_url, *, writes_stopped=False, standby_snapshot=False):
    if not writes_stopped and not standby_snapshot:
        raise ValueError('stop all SQLite writers and create a consistent snapshot before migration')
    path = Path(snapshot).resolve(strict=True)
    wal = Path(str(path) + '-wal')
    if wal.exists() and wal.stat().st_size:
        raise ValueError('snapshot must be standalone, with no pending SQLite WAL')
    source = sqlite3.connect(path.as_uri() + '?mode=ro&immutable=1', uri=True)
    source.row_factory = sqlite3.Row
    target = create_engine(destination_url, hide_parameters=True)
    if target.dialect.name != 'postgresql':
        raise ValueError('destination must be PostgreSQL')
    report = {}
    try:
        if source.execute('PRAGMA integrity_check').fetchone()[0] != 'ok':
            raise ValueError('SQLite integrity check failed')
        if source.execute('PRAGMA foreign_key_check').fetchone():
            raise ValueError('SQLite foreign key check failed')
        names = {row[0] for row in source.execute("SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'")}
        unknown = names - set(Base.metadata.tables)
        if unknown:
            raise ValueError('unmapped source tables: ' + ', '.join(sorted(unknown)))
        with target.begin() as connection:
            # A migration is maintenance work; prohibit concurrently starting apps.
            connection.execute(text('SELECT pg_advisory_xact_lock(73194002)'))
            existing = inspect(connection).get_table_names()
            unknown_target = set(existing) - set(Base.metadata.tables) - {'cluster_browser_guard'}
            if unknown_target:
                raise ValueError('destination contains unrelated tables')
            for name in existing:
                if name == 'cluster_browser_guard':
                    continue
                table = Table(name, MetaData(), autoload_with=connection)
                if connection.scalar(select(func.count()).select_from(table)):
                    raise ValueError('destination must be empty: ' + name)
            Base.metadata.create_all(connection)
            connection.exec_driver_sql('CREATE UNIQUE INDEX IF NOT EXISTS uq_users_email_lookup_hash ON users(email_lookup_hash) WHERE email_lookup_hash IS NOT NULL')
            for table in Base.metadata.sorted_tables:
                if table.name not in names:
                    continue
                quoted = '"' + table.name.replace('"', '""') + '"'
                columns = {row[1] for row in source.execute('PRAGMA table_info(' + quoted + ')')}
                if columns - set(table.c.keys()):
                    raise ValueError('unmapped columns in ' + table.name)
                # Bound memory use; original database is streamed in PK order.
                order = ', '.join('"' + c.name + '"' for c in table.primary_key)
                if not order:
                    raise ValueError('source table needs a primary key: ' + table.name)
                cursor = source.execute('SELECT * FROM ' + quoted + ' ORDER BY ' + order)
                total = 0
                while batch := cursor.fetchmany(250):
                    rows = [normalize(row, table) for row in batch]
                    connection.execute(table.insert(), rows)
                    total += len(rows)
                # Re-read both sides for an exact, normalized content comparison.
                original = (normalize(row, table) for row in source.execute('SELECT * FROM ' + quoted + ' ORDER BY ' + order))
                transferred = connection.execute(select(*[table.c[n] for n in sorted(columns)]).order_by(*table.primary_key)).mappings()
                before, after = digest(original), digest(transferred)
                if before != after:
                    raise ValueError('content verification failed: ' + table.name)
                report[table.name] = {'rows': total, 'sha256': after}
                for column in table.primary_key:
                    sequence = connection.scalar(text('SELECT pg_get_serial_sequence(:table, :column)'), {'table': table.name, 'column': column.name})
                    if sequence:
                        maximum = connection.scalar(select(func.max(column)))
                        connection.execute(text('SELECT setval(CAST(:sequence AS regclass), :value, :called)'),
                                           {'sequence': sequence, 'value': maximum or 1, 'called': maximum is not None})
        create_schema(target)
        return report
    finally:
        source.close()
        target.dispose()


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('snapshot', type=Path)
    parser.add_argument('--writes-stopped', action='store_true')
    parser.add_argument('--standby-snapshot', action='store_true',
                        help='import a consistent backup for a standby deployment; never an authoritative cutover')
    parser.add_argument('--report', type=Path, required=True)
    args = parser.parse_args()
    # Credentials are read from the environment, never command-line arguments.
    if args.standby_snapshot and os.environ.get('SPARK_STANDBY_MODE', '').lower() != 'true':
        parser.error('--standby-snapshot requires SPARK_STANDBY_MODE=true')
    report = migrate(args.snapshot, os.environ['SPARK_DATABASE_URL'], writes_stopped=args.writes_stopped,
                     standby_snapshot=args.standby_snapshot)
    args.report.write_text(json.dumps(report, indent=2), encoding='utf-8')
    print('Verified', len(report), 'tables; no source data was changed')


if __name__ == '__main__':
    main()
