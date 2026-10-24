"""Poll completed task records without changing the task executor or auth service."""
import json
import logging

from cryptography.exceptions import InvalidTag

from spark_console.db import session_scope
from spark_console.pii import PiiCipher
from spark_console.pushplus import PushPlusTransport
from spark_console.resend import ProviderResult
from spark_console.services.push_notifications import PushService


class PushNotifier:
    def __init__(self, settings, engine, transport=None):
        self.engine = engine
        self.pii = PiiCipher(settings.pii_key_file.read_bytes())
        self.transport = transport or PushPlusTransport()

    def run_once(self, now):
        with session_scope(self.engine) as db:
            service = PushService(db, self.pii)
            service.collect(now)
            claimed = service.claim(now)
            if not claimed:
                return False
            event, channel = claimed
            event_id, channel_name = event.id, channel.channel
            try:
                credentials = service.credentials_for(channel)
                payload = json.loads(self.pii.decrypt_bytes(
                    event.payload, event.nonce, aad=f"push-delivery:{event.id}".encode()))
            except (InvalidTag, ValueError, TypeError, KeyError):
                service.complete(event_id, ProviderResult(False, error_code="push_credentials"), now)
                return True
        # Never hold a database transaction during network IO.
        try:
            result = self.transport.send(credentials, channel_name, payload["title"], payload["content"])
        except Exception:
            # Do not log exceptions potentially containing a token or request body.
            logging.getLogger(__name__).warning("Push provider request failed; result is unknown")
            result = ProviderResult(False, error_code="push_unknown")
        with session_scope(self.engine) as db:
            PushService(db, self.pii).complete(event_id, result, now)
        return True
