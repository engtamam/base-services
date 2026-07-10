"""JWKS TokenVerifier — verify-only via a remote JWKS (asymmetric). Config-gated.

The JWKS URL comes from config ONLY (never a caller / token header), so a token
cannot point verification at an attacker-controlled key set (SSRF/key-confusion).
"""
from __future__ import annotations

from typing import Any

import jwt

from app.ports.errors import ConfigError, InvalidToken


class JWKSVerifier:
    driver = "jwks"

    def __init__(self, jwks_url: str, issuer: str = "", audience: str = "") -> None:
        if not jwks_url:
            raise ConfigError("TOKEN_JWKS_URL is required for the jwks driver")
        self._issuer = issuer
        self._audience = audience
        # PyJWKClient (from base pyjwt dep) caches keys and fetches on demand.
        self._client = jwt.PyJWKClient(jwks_url)

    def verify(self, token: str) -> dict[str, Any]:
        try:
            signing_key = self._client.get_signing_key_from_jwt(token)
            options = {"require": ["exp"], "verify_signature": True}
            kwargs: dict[str, Any] = {"algorithms": ["RS256"], "options": options}
            if self._issuer:
                kwargs["issuer"] = self._issuer
            if self._audience:
                kwargs["audience"] = self._audience
            else:
                options["verify_aud"] = False
            return jwt.decode(token, signing_key.key, **kwargs)
        except jwt.PyJWTError as exc:
            raise InvalidToken(type(exc).__name__) from exc
