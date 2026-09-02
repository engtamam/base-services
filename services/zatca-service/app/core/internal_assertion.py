"""Mint the short-TTL RS256 assertion this service presents when it calls ANOTHER service's /internal
tier — the caller side of the internal caller-identity seam.

iss = this service, aud = the target service, exp = now + a short TTL (default 60s, so a captured
assertion is useless within a minute and it is scoped to the ONE service it was minted for). Signed
with this service's own private key; the target verifies it against the matching public key (static
map) or this service's JWKS (by kid). Returns "" when no signing key is configured, so a caller with
the flat token still works during migration — the outbound HTTP helper simply omits the header.
"""
from __future__ import annotations

import time

import jwt


def mint_internal_assertion(*, private_key: str, issuer: str, audience: str,
                            ttl_seconds: int = 60, kid: str = "") -> str:
    """A signed assertion for calling `audience`'s /internal tier, or "" when no key is configured."""
    if not private_key or not issuer or not audience:
        return ""
    now = int(time.time())
    claims = {"iss": issuer, "aud": audience, "iat": now, "exp": now + max(1, int(ttl_seconds))}
    headers = {"kid": kid} if kid else None
    return jwt.encode(claims, private_key, algorithm="RS256", headers=headers)
