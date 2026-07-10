# =============================================================================
# {service-name} — Auth rate limiting (fail-closed)
# -----------------------------------------------------------------------------
# Keys by verified subject when present, else client IP. Over the limit -> 429.
# Fail-closed: if the limiter backend errors, deny (never fail open, CWE-703).
# =============================================================================
from __future__ import annotations

from fastapi import HTTPException, Request, status


def _client_key(request: Request) -> str:
    principal = getattr(request.state, "principal", None)
    if principal is not None and principal.subject:
        return f"sub:{principal.subject}"
    client = request.client
    return f"ip:{client.host}" if client else "ip:unknown"


async def rate_limit(request: Request) -> None:
    limiter = request.app.state.container.rate_limiter
    key = _client_key(request)
    try:
        allowed = await limiter.allow(key)
    except Exception:
        # fail-closed
        raise HTTPException(status_code=status.HTTP_429_TOO_MANY_REQUESTS, detail="RATE_LIMITED")
    if not allowed:
        raise HTTPException(status_code=status.HTTP_429_TOO_MANY_REQUESTS, detail="RATE_LIMITED")
