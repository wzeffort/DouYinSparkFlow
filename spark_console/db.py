from __future__ import annotations

from contextlib import contextmanager
import os
import stat
from pathlib import Path

from sqlalchemy import Engine, create_engine, event, text
from sqlalchemy.orm import Session

from spark_console.config import Settings
from spark_console.models import AppSetting, Base, TaskQuotaPolicy, WorkerLock
from spark_console.referral_models import ReferralLock
from spark_console import push_models
from spark_console import profile_models
from spark_console import renewal_models
from spark_console import reward_models


SQLITE_COLUMNS = {
    "users": {
        "email_ciphertext": "BLOB",
        "email_nonce": "BLOB",
        "email_lookup_hash": "VARCHAR(64)",
        "email_verified_at": "DATETIME",
        "email_updated_at": "DATETIME",
    },
    "douyin_accounts": {
        "invalidated_at": "DATETIME",
        "invalid_reason_code": "VARCHAR(48)",
        "auth_incident_id": "VARCHAR(36)",
    },
    "douyin_login_sessions": {
        "qr_crop_png": "BLOB",
    },
}


def create_engine_for(settings: Settings) -> Engine:
    options = {}
    if settings.database_url.startswith('postgresql'):
        options = {'connect_timeout': 5, 'options': '-c statement_timeout=15000 -c lock_timeout=10000'}
    engine = create_engine(
        settings.database_url,
        pool_pre_ping=True,
        hide_parameters=True,
        connect_args={"check_same_thread": False} if settings.database_url.startswith("sqlite") else options,
    )
    if settings.database_url.startswith("sqlite"):
        @event.listens_for(engine, "connect")
        def set_sqlite_pragmas(dbapi_connection, _connection_record):
            cursor = dbapi_connection.cursor()
            cursor.execute("PRAGMA foreign_keys=ON")
            cursor.execute("PRAGMA journal_mode=WAL")
            cursor.execute("PRAGMA busy_timeout=5000")
            cursor.close()
    return engine


def create_schema(engine: Engine) -> None:
    from spark_console.cluster import exclusive, SCHEMA_LOCK
    with exclusive(engine, SCHEMA_LOCK, wait=True):
        _create_schema(engine)


def _create_schema(engine: Engine) -> None:
    """Create all declared tables additively for fresh and existing databases."""
    from spark_console.registration_trials import ensure_optional_registration_invites
    ensure_optional_registration_invites(engine)
    run_additive_migrations(engine)
    Base.metadata.create_all(engine)
    if engine.dialect.name == "sqlite":
        with engine.begin() as connection:
            connection.exec_driver_sql(
                "CREATE UNIQUE INDEX IF NOT EXISTS uq_users_email_lookup_hash "
                "ON users(email_lookup_hash) WHERE email_lookup_hash IS NOT NULL"
            )
    if engine.dialect.name == 'postgresql':
        with engine.begin() as connection:
            connection.exec_driver_sql('CREATE UNIQUE INDEX IF NOT EXISTS uq_users_email_lookup_hash ON users(email_lookup_hash) WHERE email_lookup_hash IS NOT NULL')
            connection.exec_driver_sql('CREATE TABLE IF NOT EXISTS cluster_browser_guard (id INTEGER PRIMARY KEY, blocked_until TIMESTAMPTZ)')
            connection.exec_driver_sql('INSERT INTO cluster_browser_guard (id) VALUES (1) ON CONFLICT DO NOTHING')
    with session_scope(engine) as session:
        if session.get(ReferralLock, 1) is None:
            session.add(ReferralLock(id=1, version=0))
        if session.get(WorkerLock, 1) is None:
            session.add(WorkerLock(id=1))
        if session.get(TaskQuotaPolicy, 1) is None:
            session.add(
                TaskQuotaPolicy(
                    id=1,
                    default_amount=5,
                    default_duration_days=None,
                    max_saved_tasks=20,
                )
            )
        if session.get(AppSetting, "email_paused") is None:
            session.add(AppSetting(key="email_paused", value="false"))
    secure_sqlite_files(engine)


def secure_sqlite_files(engine: Engine) -> None:
    """Restrict only this SQLite database and its sidecars; never follow symlinks."""
    database = engine.url.database
    if os.name != 'posix' or engine.dialect.name != 'sqlite' or not database or database == ':memory:':
        return
    path = Path(database).absolute()
    for name in (str(path), str(path) + '-wal', str(path) + '-shm'):
        try:
            descriptor = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        except FileNotFoundError:
            continue
        try:
            metadata = os.fstat(descriptor)
            if not stat.S_ISREG(metadata.st_mode) or metadata.st_uid != os.geteuid():
                raise PermissionError('database permission hardening requires an owned regular file')
            os.fchmod(descriptor, 0o600)
        finally:
            os.close(descriptor)


def run_additive_migrations(engine: Engine) -> None:
    """Add only allow-listed nullable columns to pre-feature SQLite databases."""
    if engine.dialect.name != "sqlite":
        return
    with engine.begin() as connection:
        tables = {
            row[0]
            for row in connection.exec_driver_sql(
                "SELECT name FROM sqlite_master WHERE type='table'"
            )
        }
        for table, declarations in SQLITE_COLUMNS.items():
            if table not in tables:
                continue
            existing = {
                row[1]
                for row in connection.exec_driver_sql(f"PRAGMA table_info({table})")
            }
            for column, declaration in declarations.items():
                if column not in existing:
                    connection.exec_driver_sql(
                        f"ALTER TABLE {table} ADD COLUMN {column} {declaration}"
                    )


@contextmanager
def session_scope(engine: Engine):
    session = Session(engine, expire_on_commit=False)
    try:
        if engine.dialect.name == 'postgresql':
            # Match the original SQLite single-writer boundary across nodes.
            # Includes session revocation, quotas, registration and task claims.
            from spark_console.cluster import TRANSACTION_LOCK
            session.execute(text('SELECT pg_advisory_xact_lock(:key)'), {'key': TRANSACTION_LOCK})
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()
