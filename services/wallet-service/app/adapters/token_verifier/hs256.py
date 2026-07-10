"""HS256 TokenVerifier — symmetric shared-secret. Zero infra.

NOTE: HS256 means this service holds the SIGNING secret (it could mint tokens).
Prefer rs256/jwks for zero-trust (verify-only). Use HS256 only for simple,
single-issuer setups.
"""
from __future__ import annotations

from typing import Any

from app.adapters.token_verifier.common import decode
from app.ports.errors import ConfigError


class HS256Verifier:
    driver = "hs256"

    def __init__(self, secret: str, issuer: str = "", audience: str = "") -> None:
        if not secret or len(secret) < 32:
            raise ConfigError("TOKEN_HS256_SECRET must be set and >= 32 chars")
        self._secret = secret
        self._issuer = issuer
        self._audience = audience

    def verify(self, token: str) -> dict[str, Any]:
        return decode(
            token,
            key=self._secret,
            algorithm="HS256",
            issuer=self._issuer,
            audience=self._audience,
        )
