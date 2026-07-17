"""HmacSignedPlayback — short-lived, tenant+session+output-scoped playback tokens.

A live view is not public: HLS playlists, WebRTC endpoints, and snapshots are gated by one of these.
The token is a stateless, tamper-proof `{tenant, session, output, exp}` signed with HMAC-SHA256 —
no server state, verifiable anywhere the media origin runs, and it expires fast so a leaked URL dies.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import time
from typing import Any


def _b64(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode()


def _unb64(s: str) -> bytes:
    return base64.urlsafe_b64decode(s + "=" * (-len(s) % 4))


class HmacSignedPlayback:
    driver = "hmac"

    def __init__(self, secret: str) -> None:
        if not secret:
            raise ValueError("HmacSignedPlayback needs a signing secret")
        self._secret = secret.encode()

    def sign(self, *, tenant: str, session_id: str, output: str, ttl: int) -> str:
        payload = {"t": tenant, "s": session_id, "o": output,
                   "exp": int(time.time()) + int(ttl)}   # ttl <= 0 → already expired (a valid revoke)
        body = _b64(json.dumps(payload, separators=(",", ":"), sort_keys=True).encode())
        sig = _b64(hmac.new(self._secret, body.encode(), hashlib.sha256).digest())
        return f"{body}.{sig}"

    def verify(self, token: str) -> dict[str, Any] | None:
        try:
            body, sig = token.split(".", 1)
        except ValueError:
            return None
        expected = _b64(hmac.new(self._secret, body.encode(), hashlib.sha256).digest())
        if not hmac.compare_digest(sig, expected):     # constant-time
            return None
        try:
            payload = json.loads(_unb64(body))
        except Exception:   # noqa: BLE001
            return None
        if int(payload.get("exp", 0)) < int(time.time()):
            return None
        return {"tenant": payload.get("t"), "session_id": payload.get("s"),
                "output": payload.get("o"), "exp": payload.get("exp")}
