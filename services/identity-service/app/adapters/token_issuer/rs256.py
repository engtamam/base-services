"""RS256 token issuer — asymmetric signing (issuer holds the private key).

Two ways to supply the private key, selected at construction:

  * STATIC — a single PEM from `TOKEN_PRIVATE_KEY`. This is the historical path and it is unchanged:
    no `kid` is stamped, so the token header is byte-for-byte what it was before rotation existed.
  * KEYRING — a `key_provider` callable returning the CURRENT `(kid, private_pem)`. The issuer reads
    it on every mint, so a rotation performed elsewhere takes effect on the next token without a
    rebuild of this object, and the `kid` lets a JWKS verifier pick the matching public key.

The provider is synchronous by design: `issue_*` is on the login/refresh hot path and must not await.
The keyring service keeps the ring in memory and the provider is a cheap dict read (see
`signing_key_service.SigningKeyService.current_signing_key`).
"""
from __future__ import annotations

from typing import Any, Callable

from app.adapters.token_issuer.common import encode
from app.ports.errors import ConfigError


class RS256Issuer:
    driver = "rs256"

    def __init__(self, private_key_pem: str = "", issuer: str = "", audience: str = "", *,
                 key_provider: "Callable[[], tuple[str | None, str]] | None" = None) -> None:
        self._issuer = issuer
        self._audience = audience
        if key_provider is not None:
            self._provider = key_provider
        elif private_key_pem and "BEGIN" in private_key_pem:
            self._provider = lambda: (None, private_key_pem)
        else:
            raise ConfigError("TOKEN_PRIVATE_KEY must be a PEM private key")

    def _sign(self, subject: str, token_type: str, ttl_seconds: int,
              extra: dict[str, Any]) -> str:
        kid, key = self._provider()
        return encode(key=key, algorithm="RS256", subject=subject,
                      issuer=self._issuer, audience=self._audience,
                      ttl_seconds=ttl_seconds, token_type=token_type, extra=extra, kid=kid)

    def issue_access(self, subject: str, claims: dict[str, Any], ttl_seconds: int) -> str:
        return self._sign(subject, "access", ttl_seconds, claims)

    def issue_refresh(self, subject: str, jti: str, ttl_seconds: int) -> str:
        return self._sign(subject, "refresh", ttl_seconds, {"jti": jti})
