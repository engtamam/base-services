"""HMAC signed URLs — stateless, tamper-proof, expiring. Works with any ObjectStore.

token = base64url(json(payload)) + "." + base64url(hmac_sha256(secret, msg))
The payload carries the object key + an `exp` epoch. verify() checks the signature
(constant-time) and the expiry.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import time
from typing import Any

from app.ports.errors import ConfigError


def _b64(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).decode().rstrip("=")


def _unb64(s: str) -> bytes:
    return base64.urlsafe_b64decode(s + "=" * (-len(s) % 4))


class HmacSigner:
    driver = "hmac"

    def __init__(self, secret: str, ttl_seconds: int,
                 also_accept: tuple[str, ...] = ()) -> None:
        if not secret or len(secret) < 16:
            raise ConfigError("SIGNED_URL_SECRET must be set (>= 16 chars)")
        self._secret = secret.encode()
        self._ttl = ttl_seconds
        # Links already handed out are bearer credentials with their own expiry, and a rotation
        # cannot recall them. Verifying against the new secret alone would break every share link
        # in someone's inbox the instant the key changed — so the previous secret keeps VERIFYING
        # for its rotation window. Signing always uses the current one, so nothing new is minted
        # under a key that is on its way out.
        self._also = tuple(s.encode() for s in also_accept if s and s != secret)

    def _sign_msg(self, msg: str) -> str:
        return _b64(hmac.new(self._secret, msg.encode(), hashlib.sha256).digest())

    def sign(self, key: str, extra: dict[str, Any] | None = None) -> str:
        payload = {"key": key, "exp": int(time.time()) + self._ttl, **(extra or {})}
        msg = _b64(json.dumps(payload, separators=(",", ":")).encode())
        return f"{msg}.{self._sign_msg(msg)}"

    def verify(self, token: str) -> dict[str, Any] | None:
        try:
            msg, sig = token.split(".", 1)
        except ValueError:
            return None
        if not any(hmac.compare_digest(sig, _b64(hmac.new(k, msg.encode(), hashlib.sha256).digest()))
                   for k in (self._secret, *self._also)):
            return None
        try:
            payload = json.loads(_unb64(msg))
        except (ValueError, json.JSONDecodeError):
            return None
        if int(payload.get("exp", 0)) < int(time.time()):
            return None
        return payload
