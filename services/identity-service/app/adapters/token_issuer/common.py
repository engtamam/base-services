"""Shared JWT signing via PyJWT (vetted crypto — never hand-rolled)."""
from __future__ import annotations

import datetime as _dt
from typing import Any


def encode(
    *, key: str, algorithm: str, subject: str, issuer: str, audience: str,
    ttl_seconds: int, token_type: str, extra: dict[str, Any] | None = None,
) -> str:
    import jwt

    now = _dt.datetime.now(_dt.timezone.utc)
    payload: dict[str, Any] = {
        "sub": subject,
        "iat": now,
        "exp": now + _dt.timedelta(seconds=ttl_seconds),
        "type": token_type,
    }
    if issuer:
        payload["iss"] = issuer
    if audience:
        payload["aud"] = audience
    if extra:
        payload.update(extra)
    return jwt.encode(payload, key, algorithm=algorithm)
