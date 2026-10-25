"""Weekly account cookie validation sweep.

Read-only check per valid account against the Douyin passport identity
endpoint. Definitive unauthenticated responses (HTTP 200 + message=error)
mark the account invalid and pause its tasks, mirroring the runtime's auth
incident handling. Network failures and unexpected responses are skipped
without touching state: only a definitive verdict may change it.
"""
import json
import os
import time
from datetime import datetime, timezone

import httpx
from sqlalchemy import select, update

from spark_console.config import Settings
from spark_console.crypto import CookieCipher
from spark_console.db import create_engine_for, session_scope
from spark_console.models import DouyinAccount, SparkTask
from spark_console.services.audits import AuditService

ACCOUNT_INFO_URL = "https://www.douyin.com/passport/account/info/v2/?aid=6383"


def passport_verdict(client, header):
    """Return 'authenticated', 'unauthenticated', or 'unknown'."""
    try:
        response = client.get(
            ACCOUNT_INFO_URL,
            headers={"cookie": header, "referer": "https://www.douyin.com/"},
            timeout=8,
            follow_redirects=False,
        )
        if response.status_code != 200:
            return "unknown"
        body = response.json()
    except Exception:
        return "unknown"
    data = body.get("data") if isinstance(body, dict) else None
    if not (
        isinstance(data, dict)
        and body.get("message") == "success"
        and data.get("error_code") in (0, None)
        and data.get("user_id")
    ):
        # 200 + non-success from the passport endpoint is a definitive
        # unauthenticated verdict, not a transient failure.
        return "unauthenticated"
    return "authenticated"


def main():
    settings = Settings.from_env(os.environ)
    engine = create_engine_for(settings)
    key = bytearray(settings.cookie_key_file.read_bytes())
    try:
        cipher = CookieCipher(bytes(key))
    finally:
        key[:] = b"\0" * len(key)
        key.clear()
    now = datetime.now(timezone.utc)

    with session_scope(engine) as db:
        snapshot = [
            (a.id, a.display_name, a.encrypted_cookies, a.cookie_nonce)
            for a in db.scalars(
                select(DouyinAccount).where(DouyinAccount.validation_state == "valid")
            ).all()
        ]
    print(f"valid accounts to check: {len(snapshot)}", flush=True)
    ok = expired = unknown = 0
    with httpx.Client(trust_env=False) as client:
        for account_id, name, ciphertext, nonce in snapshot:
            try:
                raw = cipher.decrypt(ciphertext, nonce)
                envelope = json.loads(raw)
                state = (
                    envelope.get("storage_state")
                    if isinstance(envelope, dict) and envelope.get("version") == 2
                    else envelope
                )
                cookies = [
                    c
                    for c in (state or {}).get("cookies", [])
                    if isinstance(c, dict)
                    and c.get("name")
                    and isinstance(c.get("value"), str)
                    and "douyin" in str(c.get("domain", "")).lower()
                ]
                header = "; ".join(f"{c['name']}={c['value']}" for c in cookies)
            except Exception:
                print(f"{name!r}: skip (cookie decrypt failed)", flush=True)
                unknown += 1
                continue
            if not header:
                print(f"{name!r}: skip (no douyin cookies stored)", flush=True)
                unknown += 1
                continue
            verdict = passport_verdict(client, header)
            time.sleep(0.5)
            if verdict == "unknown":
                print(f"{name!r}: skip (network/unknown)", flush=True)
                unknown += 1
                continue
            with session_scope(engine) as db:
                account = db.get(DouyinAccount, account_id)
                if account is None or account.validation_state != "valid":
                    continue
                if verdict == "authenticated":
                    account.last_verified_at = now
                    print(f"{name!r}: still valid", flush=True)
                    ok += 1
                    continue
                # Definitively unauthenticated: mirror the runtime auth
                # incident handling (state + pause bound tasks) + audit.
                account.validation_state = "invalid"
                account.invalidated_at = account.invalidated_at or now
                account.invalid_reason_code = "cookie_invalid"
                db.execute(
                    update(SparkTask)
                    .where(
                        SparkTask.douyin_account_id == account.id,
                        SparkTask.enabled.is_(True),
                    )
                    .values(enabled=False, next_run_at=None)
                )
                AuditService(db).write(
                    account.owner_user_id,
                    "account.validation_expired",
                    "douyin_account",
                    account.id,
                )
                print(
                    f"{name!r}: EXPIRED -> marked invalid, bound tasks paused",
                    flush=True,
                )
                expired += 1
    print(
        f"SUMMARY: valid={ok} expired={expired} skipped={unknown}",
        flush=True,
    )


main()
