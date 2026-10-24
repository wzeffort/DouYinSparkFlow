"""PushPlus send API. A successful response means accepted, not delivered."""
import re

import httpx

from spark_console.resend import ProviderResult


class PushPlusTransport:
    ENDPOINT = "https://www.pushplus.plus/send"

    def __init__(self, client=None):
        self.client = client or httpx.Client(timeout=15, follow_redirects=False)

    def send(self, credentials, channel, title, content):
        if channel not in {"wechat", "qq"}:
            return ProviderResult(False, error_code="push_invalid_channel")
        payload = {
            "token": credentials["token"], "channel": channel,
            "title": title[:100], "content": content[:1800], "template": "txt",
        }
        if credentials.get("option"):
            payload["option"] = credentials["option"]
        try:
            response = self.client.post(self.ENDPOINT, json=payload)
        except (httpx.ConnectError, httpx.ConnectTimeout):
            return ProviderResult(False, error_code="push_connect", retryable=True)
        except httpx.HTTPError:
            # No provider idempotency contract: a timeout may follow acceptance.
            return ProviderResult(False, error_code="push_unknown")
        if not response.is_success:
            return ProviderResult(False, error_code=f"push_http_{response.status_code}",
                                  retryable=response.status_code == 429 or response.status_code >= 500)
        try:
            data = response.json()
            if not isinstance(data, dict) or type(data.get("code")) is not int:
                raise ValueError("invalid response")
        except ValueError:
            return ProviderResult(False, error_code="push_unknown")
        code = data["code"]
        if code != 200:
            # Do not persist provider messages: they may echo submitted secrets.
            return ProviderResult(False, error_code=f"push_code_{code}"[:48], retryable=code == 500)
        serial = data.get("data")
        if not isinstance(serial, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,128}", serial):
            return ProviderResult(False, error_code="push_unknown")
        return ProviderResult(True, provider_id=serial)
