"""Registration trial policy and a guarded SQLite nullable-invite migration."""
import re
from datetime import timedelta
from sqlalchemy import select
from spark_console.models import TaskQuotaGrant
from spark_console.referral_models import ReferralCode
from spark_console.services import ValidationError
from spark_console.services.audits import AuditService
from spark_console.services.task_capacity import TaskCapacityService


def apply_registration_trial(db, user, invite_id, now):
    days = 3 if invite_id is None else 7 if db.get(ReferralCode, invite_id) else 14
    service = TaskCapacityService(db, AuditService(db))
    service.bootstrap_user(user, use_current_policy=True, effective_at=now)
    grant = db.scalar(select(TaskQuotaGrant).where(
        TaskQuotaGrant.user_id == user.id, TaskQuotaGrant.is_initial.is_(True)))
    if grant is None:
        raise ValidationError("注册基础额度初始化失败")
    grant.starts_at, grant.expires_at = now, now + timedelta(days=days)
    AuditService(db).write(user.id, "registration.trial", "task_quota_grant", grant.id,
                          detail=f"days={days}")
    return days


def ensure_optional_registration_invites(engine):
    if engine.dialect.name != "sqlite":
        return
    with engine.connect() as conn:
        # Serialize inspection and rebuild so concurrent startup cannot race.
        conn.exec_driver_sql("BEGIN IMMEDIATE")
        try:
            columns = list(conn.exec_driver_sql("PRAGMA table_info(pending_registrations)"))
            if not columns or not next(c[3] for c in columns if c[1] == "invite_id"):
                conn.commit()
                return
            # Do not rebuild a table that has incoming foreign keys.
            tables = list(conn.exec_driver_sql("SELECT name FROM sqlite_master WHERE type='table'"))
            for (name,) in tables:
                quoted = '"' + name.replace('"', '""') + '"'
                if any(row[2] == "pending_registrations" for row in conn.exec_driver_sql(f"PRAGMA foreign_key_list({quoted})")):
                    raise RuntimeError("Unexpected foreign key to pending registrations")
            sql = conn.exec_driver_sql("SELECT sql FROM sqlite_master WHERE type='table' AND name='pending_registrations'").scalar_one()
            sql, replaced = re.subn(r"(?i)(\binvite_id\s+VARCHAR\(36\))\s+NOT NULL", r"\1", sql)
            if replaced != 1:
                raise RuntimeError("Unexpected pending registration schema")
            sql, renamed = re.subn(r'(?i)(CREATE TABLE\s+)(?:"pending_registrations"|pending_registrations)(?=\s*\()', r'\1pending_registrations_optional_new', sql, count=1)
            if renamed != 1:
                raise RuntimeError("Unexpected pending registration table declaration")
            indexes = [r[0] for r in conn.exec_driver_sql("SELECT sql FROM sqlite_master WHERE type='index' AND tbl_name='pending_registrations' AND sql IS NOT NULL")]
            if conn.exec_driver_sql("SELECT count(*) FROM sqlite_master WHERE type='trigger' AND tbl_name='pending_registrations'").scalar_one():
                raise RuntimeError("Unexpected pending registration triggers")
            conn.exec_driver_sql(sql)
            names = ", ".join('"' + c[1].replace('"', '""') + '"' for c in columns)
            conn.exec_driver_sql(f"INSERT INTO pending_registrations_optional_new ({names}) SELECT {names} FROM pending_registrations")
            conn.exec_driver_sql("DROP TABLE pending_registrations")
            conn.exec_driver_sql("ALTER TABLE pending_registrations_optional_new RENAME TO pending_registrations")
            for sql in indexes:
                conn.exec_driver_sql(sql)
            if list(conn.exec_driver_sql("PRAGMA foreign_key_check(pending_registrations)")):
                raise RuntimeError("Pending registration foreign key check failed")
            conn.commit()
        except Exception:
            conn.rollback()
            raise
