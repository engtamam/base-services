"""RS256 TokenVerifier — asymmetric, VERIFY-ONLY (holds only the public key).

This is the secure default: the service cannot forge tokens, only verify them.
"""
from __future__ import annotations

from typing import Any

from app.adapters.token_verifier.common import decode
from app.ports.errors import ConfigError


class RS256Verifier:
    driver = "rs256"

    def __init__(self, public_key_pem: str, issuer: str = "", audience: str = "") -> None:
        if not public_key_pem or "BEGIN" not in public_key_pem:
            raise ConfigError("TOKEN_PUBLIC_KEY must be a PEM public key")
        self._key = public_key_pem
        self._issuer = issuer
        self._audience = audience

    def verify(self, token: str) -> dict[str, Any]:
        return decode(
            token,
            key=self._key,
            algorithm="RS256",
            issuer=self._issuer,
            audience=self._audience,
        )
