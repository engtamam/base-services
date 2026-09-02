"""internal_headers — the caller side of the internal caller-identity seam, in one place.

A service builds the headers for calling ANOTHER service's /internal tier with this: the flat token
always (so nothing breaks during migration), PLUS a short-TTL RS256 assertion minted for the target
whenever this service is configured to sign (INTERNAL_ASSERTION_PRIVATE_KEY set). Every outbound
internal call routes its headers through here, so turning caller identity on is one config flip — a
key and a driver — with no edit at each call site, and a service that signs nothing keeps sending just
the token exactly as before.
"""
from __future__ import annotations

from app.core.internal_assertion import mint_internal_assertion


def internal_headers(settings, *, target: str) -> dict[str, str]:
    """Headers to call `target`'s /internal tier: X-Internal-Token always, plus X-Internal-Assertion
    (iss=this service, aud=target) when a signing key is configured. `target` is the callee's service
    name — the audience it verifies against."""
    headers = {"X-Internal-Token": settings.INTERNAL_SERVICE_TOKEN}
    assertion = mint_internal_assertion(
        private_key=settings.INTERNAL_ASSERTION_PRIVATE_KEY, issuer=settings.SERVICE_NAME,
        audience=target, ttl_seconds=settings.INTERNAL_ASSERTION_TTL_SECONDS,
        kid=settings.INTERNAL_ASSERTION_KID)
    if assertion:
        headers["X-Internal-Assertion"] = assertion
    return headers
