"""HS256 token issuer — symmetric signing (issuer holds the shared secret)."""
from __future__ import annotations

from typing import Any

from app.adapters.token_issuer.common import encode
from app.ports.errors import ConfigError


class HS256Issuer:
    driver = "hs256"

    def __init__(self, secret: str, issuer: str = "", audience: str = "") -> None:
        if not secret or len(secret) < 32:
            raise ConfigError("TOKEN_SIGNING_SECRET must be set and >= 32 chars")
        self._secret = secret
        self._issuer = issuer
        self._audience = audience

    def issue_access(self, subject: str, claims: dict[str, Any], ttl_seconds: int) -> str:
        return encode(key=self._secret, algorithm="HS256", subject=subject,
                      issuer=self._issuer, audience=self._audience,
                      ttl_seconds=ttl_seconds, token_type="access", extra=claims)

    def issue_refresh(self, subject: str, jti: str, ttl_seconds: int) -> str:
        return encode(key=self._secret, algorithm="HS256", subject=subject,
                      issuer=self._issuer, audience=self._audience,
                      ttl_seconds=ttl_seconds, token_type="refresh", extra={"jti": jti})
