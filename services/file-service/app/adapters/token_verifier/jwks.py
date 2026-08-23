"""JWKS TokenVerifier — verify-only via a remote JWKS (asymmetric). Config-gated.

The JWKS URL comes from config ONLY (never a caller / token header), so a token
cannot point verification at an attacker-controlled key set (SSRF/key-confusion).

REFRESH-ON-MISS is what makes SIGNING-KEY ROTATION work here. PyJWKClient caches the key set for
its `lifespan`, and — crucially — does NOT refetch when it sees a `kid` it has never cached: it just
fails. So the instant the issuer rotates, every verifier still holding the old set would reject the
new key's tokens for up to a full lifespan. That is a self-inflicted outage on every rotation. This
verifier instead forces ONE refresh when a token's kid is absent from the cache, then retries — so a
brand-new key is picked up immediately, while the cache still spares a fetch on the common path. The
key set is bounded, so a flood of bogus kids cannot turn this into a fetch amplifier beyond one
refresh per verify.
"""
from __future__ import annotations

from typing import Any

import jwt

from app.ports.errors import ConfigError, InvalidToken

# Cache the fetched key set this long. Short enough that a RETIRED key drops out promptly (a
# compromised-then-rotated key should stop verifying soon), long enough to spare a fetch per verify.
# New keys do not wait for this — see the refresh-on-miss note above.
_DEFAULT_CACHE_LIFESPAN_S = 300


class JWKSVerifier:
    driver = "jwks"

    def __init__(self, jwks_url: str, issuer: str = "", audience: str = "", *,
                 cache_lifespan_s: float = _DEFAULT_CACHE_LIFESPAN_S) -> None:
        if not jwks_url:
            raise ConfigError("TOKEN_JWKS_URL is required for the jwks driver")
        self._issuer = issuer
        self._audience = audience
        # PyJWKClient (from base pyjwt dep) caches the key set and fetches on demand.
        self._client = jwt.PyJWKClient(jwks_url, lifespan=cache_lifespan_s)

    def _signing_key(self, token: str):
        """The signing key for this token's kid, forcing a single JWKS refresh if it is unknown —
        the new key from a rotation the cached set predates."""
        kid = jwt.get_unverified_header(token).get("kid")
        if not kid:
            # No kid (e.g. a single-key issuer): fall back to the client's own resolution.
            return self._client.get_signing_key_from_jwt(token).key
        try:
            return self._client.get_signing_key(kid).key
        except jwt.PyJWKClientError:
            self._client.get_jwk_set(refresh=True)   # a rotation may have added this kid
            return self._client.get_signing_key(kid).key

    def verify(self, token: str) -> dict[str, Any]:
        try:
            key = self._signing_key(token)
            options = {"require": ["exp"], "verify_signature": True}
            kwargs: dict[str, Any] = {"algorithms": ["RS256"], "options": options}
            if self._issuer:
                kwargs["issuer"] = self._issuer
            if self._audience:
                kwargs["audience"] = self._audience
            else:
                options["verify_aud"] = False
            return jwt.decode(token, key, **kwargs)
        except jwt.PyJWTError as exc:
            raise InvalidToken(type(exc).__name__) from exc
