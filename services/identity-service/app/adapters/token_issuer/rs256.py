"""RS256 token issuer — asymmetric signing (issuer holds the private key)."""
from __future__ import annotations

from typing import Any

from app.adapters.token_issuer.common import encode
from app.ports.errors import ConfigError


class RS256Issuer:
    driver = "rs256"

    def __init__(self, private_key_pem: str, issuer: str = "", audience: str = "") -> None:
        if not private_key_pem or "BEGIN" not in private_key_pem:
            raise ConfigError("TOKEN_PRIVATE_KEY must be a PEM private key")
        self._key = private_key_pem
        self._issuer = issuer
        self._audience = audience

    def issue_access(self, subject: str, claims: dict[str, Any], ttl_seconds: int) -> str:
        return encode(key=self._key, algorithm="RS256", subject=subject,
                      issuer=self._issuer, audience=self._audience,
                      ttl_seconds=ttl_seconds, token_type="access", extra=claims)

    def issue_refresh(self, subject: str, jti: str, ttl_seconds: int) -> str:
        return encode(key=self._key, algorithm="RS256", subject=subject,
                      issuer=self._issuer, audience=self._audience,
                      ttl_seconds=ttl_seconds, token_type="refresh", extra={"jti": jti})
