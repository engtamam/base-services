"""The caller side of the internal caller-identity seam — attach a verifiable identity to outbound
calls, in one place.

`assertion_headers(settings, target=)` returns the X-Internal-Assertion header to ADD to a call to
`target`'s /internal tier (empty when this service has no signing key). It is deliberately ADD-ONLY:
the existing X-Internal-Token each call site already sets is left untouched, so wiring this in never
changes which token is sent — it only layers verifiable identity on top, and a service that signs
nothing behaves exactly as before. `internal_headers` is the convenience form for a green-field call
site that wants both the shared token and the assertion.

The minted assertion is CACHED per (issuer, target, kid) until shortly before it expires, so a hot
path (every audit event, say) does not pay an RS256 signature per call — it re-signs about once per
TTL, not once per request.
"""
from __future__ import annotations

import time

from app.core.internal_assertion import mint_internal_assertion

_MARGIN_SECONDS = 5
_cache: dict[tuple, tuple[float, str]] = {}


def _assertion(settings, target: str) -> str:
    if not settings.INTERNAL_ASSERTION_PRIVATE_KEY or not target:
        return ""
    # Include the key itself in the cache identity, so a rotated signing key (even without a kid
    # change) is never served from a stale entry — and so tests with distinct keys never collide.
    key = (settings.SERVICE_NAME, target, settings.INTERNAL_ASSERTION_KID,
           hash(settings.INTERNAL_ASSERTION_PRIVATE_KEY))
    now = time.time()
    hit = _cache.get(key)
    if hit is not None and hit[0] > now:
        return hit[1]
    ttl = max(1, int(settings.INTERNAL_ASSERTION_TTL_SECONDS))
    assertion = mint_internal_assertion(
        private_key=settings.INTERNAL_ASSERTION_PRIVATE_KEY, issuer=settings.SERVICE_NAME,
        audience=target, ttl_seconds=ttl, kid=settings.INTERNAL_ASSERTION_KID)
    if assertion:
        # Expire the cache entry a margin BEFORE the token itself, so a cached assertion is never
        # served in the last seconds of its life (where clock skew could make the callee reject it).
        _cache[key] = (now + max(1, ttl - _MARGIN_SECONDS), assertion)
    return assertion


def assertion_headers(settings, *, target: str) -> dict[str, str]:
    """The assertion header to ADD to an outbound /internal call to `target` (iss=this service,
    aud=target), or {} when this service signs nothing. Merge into the call's existing headers."""
    assertion = _assertion(settings, target)
    return {"X-Internal-Assertion": assertion} if assertion else {}


def internal_headers(settings, *, target: str) -> dict[str, str]:
    """Both headers for a green-field call to `target`'s /internal tier: the shared token plus the
    assertion (when signing is configured)."""
    return {"X-Internal-Token": settings.INTERNAL_SERVICE_TOKEN,
            **assertion_headers(settings, target=target)}
