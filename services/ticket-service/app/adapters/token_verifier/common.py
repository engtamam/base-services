"""Shared JWT verification via PyJWT (vetted crypto — never hand-rolled).

Pins the algorithm to a single value (defeats algorithm-confusion / alg=none,
CWE-347), requires a valid signature, and checks issuer/audience when configured.
"""
from __future__ import annotations

from typing import Any

import jwt

from app.ports.errors import InvalidToken


def decode(
    token: str,
    *,
    key: str,
    algorithm: str,
    issuer: str,
    audience: str,
) -> dict[str, Any]:
    options = {
        "require": ["exp"],  # tokens must expire
        "verify_signature": True,
        "verify_exp": True,
    }
    kwargs: dict[str, Any] = {
        "algorithms": [algorithm],  # EXACTLY one — no negotiation
        "options": options,
    }
    if issuer:
        kwargs["issuer"] = issuer
    if audience:
        kwargs["audience"] = audience
    else:
        # No audience configured -> don't demand one, but still verify if present.
        options["verify_aud"] = False
    try:
        return jwt.decode(token, key, **kwargs)
    except jwt.PyJWTError as exc:  # expired, bad sig, wrong iss/aud, malformed, ...
        raise InvalidToken(type(exc).__name__) from exc
