from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Mapping


@dataclass(frozen=True)
class Settings:
    data_dir: Path
    database_url: str
    cookie_key_file: Path
    session_key_file: Path
    timezone: str = "Asia/Shanghai"
    web_bind: str = "127.0.0.1"
    web_port: int = 8899
    worker_poll_seconds: int = 10
    auth_poll_seconds: int = 1
    auth_warm_max_age_seconds: int = 120
    clock_offset_limit_seconds: int = 5
    secure_cookies: bool = True
    pii_key_file: Path | None = None
    public_base_url: str = ""
    email_enabled: bool = False
    email_poll_seconds: int = 10
    resend_api_key: str = field(default="", repr=False)
    resend_from: str = ""
    health_snapshot_path: Path | None = None

    @classmethod
    def from_env(cls, environ: Mapping[str, str]) -> "Settings":
        try:
            data_dir = Path(environ["SPARK_DATA_DIR"]).resolve()
            cookie_key_file = Path(environ["SPARK_COOKIE_KEY_FILE"]).resolve()
            session_key_file = Path(environ["SPARK_SESSION_KEY_FILE"]).resolve()
        except KeyError as error:
            raise ValueError(f"missing required setting: {error.args[0]}") from error

        data_dir.mkdir(parents=True, exist_ok=True)
        if len(cookie_key_file.read_bytes()) != 32:
            raise ValueError("cookie key must be exactly 32 bytes")
        if len(session_key_file.read_bytes()) < 32:
            raise ValueError("session key must contain at least 32 bytes")

        email_enabled = environ.get("SPARK_EMAIL_ENABLED", "false").strip().lower() in {
            "1", "true", "yes", "on"
        }
        pii_key_file = None
        public_base_url = environ.get("SPARK_PUBLIC_BASE_URL", "").strip().rstrip("/")
        resend_api_key = environ.get("RESEND_API_KEY", "").strip()
        resend_from = environ.get("RESEND_FROM", "").strip()
        if email_enabled:
            raw_pii_path = environ.get("SPARK_PII_KEY_FILE", "").strip()
            if not raw_pii_path:
                raise ValueError("missing required setting: SPARK_PII_KEY_FILE")
            pii_key_file = Path(raw_pii_path).resolve()
            if len(pii_key_file.read_bytes()) != 32:
                raise ValueError("PII key must be exactly 32 bytes")
            if not public_base_url.startswith("https://"):
                raise ValueError("SPARK_PUBLIC_BASE_URL must use HTTPS")
            if not resend_api_key or not resend_from:
                raise ValueError("email requires RESEND_API_KEY and RESEND_FROM")

        return cls(
            data_dir=data_dir,
            database_url=environ.get(
                "SPARK_DATABASE_URL", f"sqlite:///{data_dir / 'spark.db'}"
            ),
            cookie_key_file=cookie_key_file,
            session_key_file=session_key_file,
            timezone=environ.get("SPARK_TIMEZONE", "Asia/Shanghai"),
            web_bind=environ.get("SPARK_WEB_BIND", "127.0.0.1"),
            web_port=int(environ.get("SPARK_WEB_PORT", "8899")),
            worker_poll_seconds=int(environ.get("SPARK_WORKER_POLL_SECONDS", "10")),
            auth_poll_seconds=max(
                1, int(environ.get("SPARK_AUTH_POLL_SECONDS", "1"))
            ),
            auth_warm_max_age_seconds=max(
                30, int(environ.get("SPARK_AUTH_WARM_MAX_AGE_SECONDS", "120"))
            ),
            clock_offset_limit_seconds=int(
                environ.get("SPARK_CLOCK_OFFSET_LIMIT_SECONDS", "5")
            ),
            secure_cookies=environ.get("SPARK_SECURE_COOKIES", "true").strip().lower()
            not in {"0", "false", "no", "off"},
            pii_key_file=pii_key_file,
            public_base_url=public_base_url,
            email_enabled=email_enabled,
            email_poll_seconds=max(
                1, int(environ.get("SPARK_EMAIL_POLL_SECONDS", "10"))
            ),
            resend_api_key=resend_api_key,
            resend_from=resend_from,
            health_snapshot_path=(
                Path(environ["SPARK_HEALTH_SNAPSHOT_PATH"]).resolve()
                if environ.get("SPARK_HEALTH_SNAPSHOT_PATH", "").strip()
                else None
            ),
        )
