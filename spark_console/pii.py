from __future__ import annotations

import hashlib
import hmac
import os
import re

from cryptography.hazmat.primitives.ciphers.aead import AESGCM


EMAIL_PATTERN = re.compile(r"^[^\s@\x00-\x1f\x7f]+@[^\s@\x00-\x1f\x7f]+$")


def normalize_email(value: str) -> str:
    clean = str(value or "").strip().lower()
    if not (3 <= len(clean) <= 254) or not EMAIL_PATTERN.fullmatch(clean):
        raise ValueError("invalid email")
    local, domain = clean.rsplit("@", 1)
    if (
        not local
        or len(local) > 64
        or "." not in domain
        or domain.startswith(".")
        or domain.endswith(".")
        or ".." in domain
    ):
        raise ValueError("invalid email")
    return clean


def mask_email(value: str) -> str:
    local, domain = normalize_email(value).rsplit("@", 1)
    if len(local) <= 2:
        masked = local[0] + "*"
    elif len(local) <= 4:
        masked = local[0] + "*" * (len(local) - 1)
    else:
        masked = local[:2] + "*" * (len(local) - 4) + local[-2:]
    return f"{masked}@{domain}"


class PiiCipher:
    def __init__(self, key: bytes):
        if len(key) != 32:
            raise ValueError("PII key must be exactly 32 bytes")
        self._key = key
        self._aead = AESGCM(key)

    def encrypt_email(self, email: str, *, aad: bytes) -> tuple[bytes, bytes]:
        return self.encrypt_bytes(normalize_email(email).encode("utf-8"), aad=aad)

    def decrypt_email(self, ciphertext: bytes, nonce: bytes, *, aad: bytes) -> str:
        return self.decrypt_bytes(ciphertext, nonce, aad=aad).decode("utf-8")

    def encrypt_bytes(self, value: bytes, *, aad: bytes) -> tuple[bytes, bytes]:
        nonce = os.urandom(12)
        return self._aead.encrypt(nonce, value, aad), nonce

    def decrypt_bytes(self, ciphertext: bytes, nonce: bytes, *, aad: bytes) -> bytes:
        return self._aead.decrypt(nonce, ciphertext, aad)

    def lookup_hash(self, email: str) -> str:
        return self._digest(b"email-lookup\0", normalize_email(email))

    def code_hash(self, scope: str, code: str) -> str:
        return self._digest(b"verification-code\0", f"{scope}\0{code}")

    def verify_code(self, scope: str, code: str, digest: str) -> bool:
        return hmac.compare_digest(self.code_hash(scope, code), digest)

    def token_hash(self, scope: str, token: str) -> str:
        return self._digest(b"action-token\0", f"{scope}\0{token}")

    def _digest(self, prefix: bytes, value: str) -> str:
        return hmac.new(
            self._key, prefix + value.encode("utf-8"), hashlib.sha256
        ).hexdigest()
