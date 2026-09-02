"""AssertionCaller — verifiable per-caller identity from a short-TTL RS256 assertion.

The caller presents X-Internal-Assertion: an RS256 JWT with iss=its own service name, aud=THIS
service, and a short exp. We verify the signature against the caller's key (resolved by iss), require
aud==this service and a valid exp, and return CallerIdentity(caller=iss). Two key sources share this
one verifier — a static provisioned public key per caller, or the caller's JWKS — because only WHERE
the key comes from differs, not how the assertion is checked.

DUAL-ACCEPT for a no-migration rollout: with allow_token_fallback on (the default while callers are
still being taught to sign), a request with no assertion but the correct flat token is still accepted
as an unnamed peer — so enabling this driver fleet-wide breaks nothing, and a deployment flips the
fallback off once its callers all sign. Every failure mode raises the SAME InternalCallerError, so a
caller never learns whether it was a missing assertion, a wrong audience, an expired token, or a bad
signature.
"""
from __future__ import annotations

import hmac
from typing import Protocol

import jwt

from app.ports.internal_caller import CallerIdentity, InternalCallerError


class KeyResolver(Protocol):
    driver: str

    def key(self, issuer: str, assertion: str):
        """The verification key for `issuer` (a PEM string or a cryptographic key object), or None if
        this issuer is not a recognised caller. `assertion` is available for kid-based (JWKS) lookup."""
        ...


class StaticKeyResolver:
    """iss → a statically-provisioned PEM public key. No runtime fetch, so verification never depends
    on reaching the caller — the most robust posture for an auth check on the request path."""
    driver = "static"

    def __init__(self, keys: dict[str, str]) -> None:
        self._keys = {k: v for k, v in (keys or {}).items() if k and v}

    def key(self, issuer: str, assertion: str):
        return self._keys.get(issuer)


class JwksKeyResolver:
    """iss → the caller's JWKS URL, resolving the signing key by the assertion's kid (reuses the
    fleet's key-rotation story). One PyJWKClient per caller, each caching its key set."""
    driver = "jwks"

    def __init__(self, urls: dict[str, str], *, cache_lifespan_s: int = 300) -> None:
        self._clients = {k: jwt.PyJWKClient(v, lifespan=cache_lifespan_s)
                         for k, v in (urls or {}).items() if k and v}

    def key(self, issuer: str, assertion: str):
        client = self._clients.get(issuer)
        if client is None:
            return None
        try:
            return client.get_signing_key_from_jwt(assertion).key
        except jwt.PyJWKClientError:
            return None


class AssertionCaller:
    def __init__(self, resolver: KeyResolver, *, allow_token_fallback: bool = True,
                 expected_token: str = "", leeway_seconds: int = 10) -> None:
        self._resolver = resolver
        self.driver = resolver.driver
        self._allow_fallback = allow_token_fallback
        self._expected = expected_token or ""
        self._leeway = max(0, leeway_seconds)

    def verify(self, *, token: str, assertion: str, audience: str) -> CallerIdentity:
        if assertion:
            return self._verify_assertion(assertion, audience)
        # No assertion presented: during migration, the flat token still authenticates an unnamed peer.
        if self._allow_fallback and self._expected and hmac.compare_digest(token or "", self._expected):
            return CallerIdentity(caller="", via="shared")
        raise InternalCallerError("no valid caller assertion")

    def _verify_assertion(self, assertion: str, audience: str) -> CallerIdentity:
        try:
            issuer = str(jwt.decode(assertion, options={"verify_signature": False}).get("iss", ""))
        except Exception as exc:  # noqa: BLE001 — a malformed token is just an unauthenticated caller
            raise InternalCallerError("unparseable assertion") from exc
        key = self._resolver.key(issuer, assertion) if issuer else None
        if key is None:
            raise InternalCallerError("unknown caller")
        try:
            claims = jwt.decode(
                assertion, key=key, algorithms=["RS256"], audience=audience,
                leeway=self._leeway, options={"require": ["exp", "iss", "aud"]})
        except Exception as exc:  # noqa: BLE001 — bad sig / wrong aud / expired all deny identically
            raise InternalCallerError("assertion rejected") from exc
        return CallerIdentity(caller=str(claims.get("iss", "")), via=self.driver)
