from __future__ import annotations

from contextlib import contextmanager

from sqlalchemy import Engine, create_engine, event
from sqlalchemy.orm import Session

from spark_console.config import Settings
from spark_console.models import AppSetting, Base, TaskQuotaPolicy, WorkerLock


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
    engine = create_engine(
        settings.database_url,
        connect_args={"check_same_thread": False} if settings.database_url.startswith("sqlite") else {},
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
    """Create all declared tables additively for fresh and existing databases."""
    run_additive_migrations(engine)
    Base.metadata.create_all(engine)
    if engine.dialect.name == "sqlite":
        with engine.begin() as connection:
            connection.exec_driver_sql(
                "CREATE UNIQUE INDEX IF NOT EXISTS uq_users_email_lookup_hash "
                "ON users(email_lookup_hash) WHERE email_lookup_hash IS NOT NULL"
            )
    with session_scope(engine) as session:
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
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()
