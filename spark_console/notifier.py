from __future__ import annotations

import json
import os
import signal
import socket
import time
from datetime import datetime, timezone

from spark_console.config import Settings
from spark_console.db import create_engine_for, create_schema, session_scope
from spark_console.pii import PiiCipher
from spark_console.resend import ResendTransport
from spark_console.services.audits import AuditService
from spark_console.services.email_verification import EmailVerificationService
from spark_console.services.notifications import NotificationService


class Notifier:
    def __init__(self, settings: Settings, engine, transport=None):
        if not settings.email_enabled or settings.pii_key_file is None:
            raise ValueError("email notifier is disabled")
        self.settings = settings
        self.engine = engine
        self.pii = PiiCipher(settings.pii_key_file.read_bytes())
        self.transport = transport or ResendTransport(
            settings.resend_api_key, settings.resend_from, settings.public_base_url
        )
        self.worker_id = f"{socket.gethostname()}-{os.getpid()}"
        self._last_cleanup_date = None

    def run_once(self, now=None) -> bool:
        current = now or datetime.now(timezone.utc)
        with session_scope(self.engine) as db:
            service = NotificationService(db, self.pii, AuditService(db))
            service.recover_stale(current)
            if self._last_cleanup_date != current.date():
                EmailVerificationService(
                    db, None, self.pii, AuditService(db)  # cleanup does not use passwords
                ).cleanup_expired(current)
                self._last_cleanup_date = current.date()
            event = service.claim_due(self.worker_id, current)
            if event is None:
                return False
            event_id = event.id
            recipient = service.recipient_for(event)
            template_key = event.template_key
            payload = service.payload_for(event)
        result = self.transport.send(event_id, recipient, template_key, payload)
        recipient = ""
        with session_scope(self.engine) as db:
            service = NotificationService(db, self.pii, AuditService(db))
            if result.success:
                service.mark_sent(event_id, result.provider_id or "accepted", current)
            else:
                service.mark_failed(
                    event_id, result.error_code or "provider_error", result.retryable, current
                )
        return True


def run_loop() -> None:
    settings = Settings.from_env(os.environ)
    engine = create_engine_for(settings)
    create_schema(engine)
    notifier = Notifier(settings, engine)
    stopping = False

    def stop(_signum, _frame):
        nonlocal stopping
        stopping = True

    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    while not stopping:
        worked = notifier.run_once()
        if not worked:
            time.sleep(settings.email_poll_seconds)


if __name__ == "__main__":
    run_loop()
