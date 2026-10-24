from __future__ import annotations

import json
import re
import urllib.parse

import httpx
from sqlalchemy import delete, select, update
from sqlalchemy.orm import Session

from spark_console.credentials import CredentialError, CredentialPayload
from spark_console.crypto import CookieCipher
from spark_console.models import (
    DouyinAccount,
    DouyinAccountIdentity,
    DouyinContactIdentity,
    DouyinConversation,
    SparkTask,
    UserNotification,
    utc_now,
)
from spark_console.services import NotFound, ValidationError
from spark_console.services.audits import AuditService
from spark_console.services.contacts import merge_contacts


ACCOUNT_INFO_URL = "https://www.douyin.com/passport/account/info/v2/?aid=6383"


def _clean_name(value):
    if not isinstance(value, str):
        return None
    value = value.strip()
    return value if 1 <= len(value) <= 64 and all(ord(c) >= 32 for c in value) else None


def _is_placeholder_name(value: str | None) -> bool:
    # Passport "name"/"screen_name" default to "user<id>" for accounts that
    # never set them; that is not the Douyin display nickname.
    return bool(value) and re.fullmatch("user[0-9]+", value) is not None


def _walk_self_objects(payload, sec_user_id, hits, depth=0):
    if depth > 12 or len(hits) >= 5:
        return
    if isinstance(payload, dict):
        if any(
            isinstance(v, str) and len(v) > 40 and v == sec_user_id
            for v in payload.values()
        ):
            hits.append(payload)
        for value in payload.values():
            _walk_self_objects(value, sec_user_id, hits, depth + 1)
    elif isinstance(payload, list):
        for value in payload:
            _walk_self_objects(value, sec_user_id, hits, depth + 1)


def _self_nickname_from_chat_html(html: str, sec_user_id: str) -> str | None:
    if not sec_user_id:
        return None
    marker = 'id="RENDER_DATA"'
    pos = html.find(marker)
    while pos != -1:
        start = html.find(">", pos) + 1
        end = html.find("</script>", start)
        if start and end > start:
            try:
                payload = json.loads(urllib.parse.unquote(html[start:end]))
            except Exception:
                payload = None
            if payload is not None:
                hits: list = []
                _walk_self_objects(payload, sec_user_id, hits)
                for obj in hits:
                    for key in ("nickname", "nick_name", "nickName"):
                        name = _clean_name(obj.get(key))
                        if name and not _is_placeholder_name(name):
                            return name
        pos = html.find(marker, pos + 1)
    return None


def _douyin_display_name(storage_state: dict) -> str | None:
    """Read the bound account's own Douyin nickname with its stored cookies.

    The passport identity endpoint validates the session but its name fields
    are "user<id>" placeholders for most accounts, so the real display
    nickname is read from the chat page's embedded RENDER_DATA, anchored to
    the session's own sec_user_id. Unauthenticated responses are rejected via
    the user_id guard.
    """
    cookies = [
        cookie
        for cookie in (storage_state or {}).get("cookies", [])
        if isinstance(cookie, dict)
        and cookie.get("name")
        and isinstance(cookie.get("value"), str)
        and "douyin" in str(cookie.get("domain", "")).lower()
    ]
    if not cookies:
        return None
    header = "; ".join(f"{c['name']}={c['value']}" for c in cookies)
    try:
        response = httpx.get(
            ACCOUNT_INFO_URL,
            headers={"cookie": header, "referer": "https://www.douyin.com/"},
            timeout=8,
            follow_redirects=False,
        )
        if response.status_code != 200:
            return None
        body = response.json()
    except Exception:
        return None
    data = body.get("data") if isinstance(body, dict) else None
    if not (
        isinstance(data, dict)
        and body.get("message") == "success"
        and data.get("error_code") in (0, None)
        and data.get("user_id")
    ):
        return None
    passport_name = _clean_name(data.get("nickname")) or _clean_name(data.get("name"))
    try:
        chat = httpx.get(
            "https://www.douyin.com/chat",
            headers={
                "cookie": header,
                "referer": "https://www.douyin.com/",
                "user-agent": (
                    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"
                    " (KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
                ),
            },
            timeout=12,
            follow_redirects=False,
        )
        if chat.status_code == 200:
            nickname = _self_nickname_from_chat_html(
                chat.text, data.get("sec_user_id") or ""
            )
            if nickname:
                return nickname
    except Exception:
        pass
    if passport_name and not _is_placeholder_name(passport_name):
        return passport_name
    return None


class AccountService:
    def __init__(self, session: Session, cipher: CookieCipher, audit: AuditService):
        self.session = session
        self.cipher = cipher
        self.audit = audit

    def create(self, owner_id: str, display_name: str, cookies: bytes | str) -> DouyinAccount:
        name = self._validated_display_name(display_name)
        raw = cookies.encode("utf-8") if isinstance(cookies, str) else cookies
        try:
            parsed = json.loads(raw)
        except (ValueError, UnicodeDecodeError) as error:
            raise ValidationError("Cookie 必须是有效的 JSON") from error
        if not isinstance(parsed, list) or not parsed:
            raise ValidationError("Cookie JSON 必须是非空数组")
        sealed = self.cipher.encrypt(raw)
        account = DouyinAccount(
            owner_user_id=owner_id,
            display_name=name,
            encrypted_cookies=sealed.ciphertext,
            cookie_nonce=sealed.nonce,
        )
        self.session.add(account)
        self.session.flush()
        self.audit.write(owner_id, "account.created", "douyin_account", account.id)
        return account

    def create_from_storage_state(
        self,
        owner_id: str,
        display_name: str,
        storage_state: dict,
        douyin_unique_id: str | None = None,
        conversation_names=(),
        contact_identities=(),
    ) -> DouyinAccount:
        name = self._validated_display_name(display_name)
        normalized_unique_id = (
            douyin_unique_id.strip() if douyin_unique_id is not None else None
        )
        if normalized_unique_id == "":
            normalized_unique_id = None
        if normalized_unique_id is not None and len(normalized_unique_id) > 64:
            raise ValidationError("抖音号不能超过 64 个字符")

        envelope = {"version": 2, "storage_state": storage_state}
        try:
            raw = json.dumps(
                envelope, ensure_ascii=False, separators=(",", ":")
            ).encode("utf-8")
            CredentialPayload.parse(raw, 2)
        except (CredentialError, TypeError, ValueError, UnicodeEncodeError):
            raise ValidationError("浏览器凭据格式无效") from None
        sealed = self.cipher.encrypt(raw)
        account = None
        if normalized_unique_id is not None:
            account = self.session.scalar(
                select(DouyinAccount)
                .join(
                    DouyinAccountIdentity,
                    DouyinAccountIdentity.account_id == DouyinAccount.id,
                )
                .where(
                    DouyinAccount.owner_user_id == owner_id,
                    DouyinAccountIdentity.douyin_unique_id
                    == normalized_unique_id,
                )
                .order_by(DouyinAccount.updated_at.desc())
                .limit(1)
            )
        reused = account is not None
        if reused:
            previous_incident_id = account.auth_incident_id
            # A temporary lookup failure must not erase an existing nickname/remark.
            if name != "抖音账号" or account.display_name == "抖音账号":
                account.display_name = name
            account.encrypted_cookies = sealed.ciphertext
            account.cookie_nonce = sealed.nonce
            account.cookie_version = 2
            account.validation_state = "valid"
            account.last_verified_at = utc_now()
            account.invalidated_at = None
            account.invalid_reason_code = None
            account.auth_incident_id = None
            if previous_incident_id:
                self.session.execute(
                    update(UserNotification)
                    .where(
                        UserNotification.user_id == owner_id,
                        UserNotification.dedupe_key
                        == f"douyin-auth:{previous_incident_id}:in-app",
                        UserNotification.read_at.is_(None),
                    )
                    .values(read_at=utc_now())
                )
        else:
            account = DouyinAccount(
                owner_user_id=owner_id,
                display_name=name,
                encrypted_cookies=sealed.ciphertext,
                cookie_nonce=sealed.nonce,
                cookie_version=2,
                validation_state="valid",
                last_verified_at=utc_now(),
            )
            self.session.add(account)
            self.session.flush()
            self.session.add(
                DouyinAccountIdentity(
                    account_id=account.id, douyin_unique_id=normalized_unique_id
                )
            )
        merge_contacts(self.session, account.id, conversation_names, contact_identities)
        self.audit.write(
            owner_id,
            "account.rebound" if reused else "account.created",
            "douyin_account",
            account.id,
        )
        return account

    def rename_owned(
        self, owner_id: str, account_id: str, display_name: str
    ) -> DouyinAccount:
        account = self.get_owned(owner_id, account_id)
        account.display_name = self._validated_display_name(display_name)
        self.session.flush()
        self.audit.write(owner_id, "account.renamed", "douyin_account", account.id)
        return account

    def refresh_display_name_owned(self, owner_id: str, account_id: str) -> DouyinAccount:
        account = self.get_owned(owner_id, account_id)
        raw = self.cipher.decrypt(account.encrypted_cookies, account.cookie_nonce)
        try:
            envelope = json.loads(raw)
        except (ValueError, UnicodeDecodeError) as error:
            raise ValidationError("账号凭据无效，请重新绑定") from error
        storage_state = (
            envelope.get("storage_state")
            if isinstance(envelope, dict) and envelope.get("version") == 2
            else envelope
        )
        name = _douyin_display_name(storage_state if isinstance(storage_state, dict) else {})
        if name is None:
            raise ValidationError("暂时无法获取昵称，请稍后重试；登录失效时请重新扫码绑定")
        account.display_name = self._validated_display_name(name)
        self.session.flush()
        self.audit.write(owner_id, "account.name_refreshed", "douyin_account", account.id)
        return account

    def get_owned(self, owner_id: str, account_id: str) -> DouyinAccount:
        account = self.session.scalar(
            select(DouyinAccount).where(
                DouyinAccount.id == account_id,
                DouyinAccount.owner_user_id == owner_id,
            )
        )
        if account is None:
            raise NotFound("account not found")
        return account

    def list_owned(self, owner_id: str) -> list[dict[str, str | None]]:
        accounts = self.session.scalars(
            select(DouyinAccount)
            .where(DouyinAccount.owner_user_id == owner_id)
            .order_by(DouyinAccount.created_at)
        ).all()
        identities = dict(self.session.execute(
            select(DouyinAccountIdentity.account_id, DouyinAccountIdentity.douyin_unique_id)
            .join(DouyinAccount, DouyinAccount.id == DouyinAccountIdentity.account_id)
            .where(DouyinAccount.owner_user_id == owner_id)
        ).all())
        return [
            {"id": item.id, "display_name": item.display_name,
             "account_label": (f"{item.display_name} · {identities[item.id]}"
                               if identities.get(item.id) else item.display_name),
             "validation_state": item.validation_state,
             "invalid_reason_code": item.invalid_reason_code}
            for item in accounts
        ]

    def decrypt_for_worker(self, account_id: str) -> bytearray:
        account = self.session.get(DouyinAccount, account_id)
        if account is None:
            raise NotFound("account not found")
        return bytearray(self.cipher.decrypt(account.encrypted_cookies, account.cookie_nonce))

    def delete_owned(self, owner_id: str, account_id: str) -> None:
        account = self.get_owned(owner_id, account_id)
        self.session.execute(
            update(SparkTask)
            .where(SparkTask.douyin_account_id == account.id)
            .values(enabled=False, douyin_account_id=None)
        )
        account.encrypted_cookies = b""
        account.cookie_nonce = b""
        self.session.flush()
        self.session.delete(account)
        self.audit.write(owner_id, "account.deleted", "douyin_account", account_id)

    @staticmethod
    def _validated_display_name(display_name: str) -> str:
        name = display_name.strip()
        if not name or len(name) > 64:
            raise ValidationError("账号名称须为 1–64 个字符")
        return name


def _limited(value, length: int) -> str | None:
    text = str(value or "").strip()
    return text[:length] or None
