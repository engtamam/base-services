# =============================================================================
# identity-service — Rate limiting (policy-driven, honest headers)
# -----------------------------------------------------------------------------
# The POLICY decides the key (composite: subject / ip / tenant / route) and which
# budget applies (per-route class + cost). The LIMITER decides if the key is over
# it. Every response carries RateLimit-* so a well-behaved client can pace itself
# BEFORE it is ever rejected; a 429 additionally carries Retry-After, because a
# client told "too many requests" with no "try again in Ns" retries immediately,
# and a herd of those is a DoS you built yourself (phase-32 T10).
# =============================================================================
from __future__ import annotations

from fastapi import Depends, HTTPException, Request, Response, status

from app.api.auth import Principal, _extract_principal
from app.config import get_settings
from app.core.ratelimit_policy import RateLimitPolicy

def build_policy(settings) -> RateLimitPolicy:
    """Built once at app creation and stored on app.state — never a module global, so it cannot go
    stale across a config reload or (in tests) across two apps with different limits."""
    return RateLimitPolicy.from_config(
        key_parts=settings.RATELIMIT_KEY_PARTS, routes_json=settings.RATELIMIT_ROUTES,
        default_limit=settings.RATELIMIT_MAX, default_window_s=settings.RATELIMIT_WINDOW_SECONDS)


def _policy(request: Request) -> RateLimitPolicy:
    pol = getattr(request.app.state, "ratelimit_policy", None)
    if pol is None:
        pol = build_policy(get_settings())
        request.app.state.ratelimit_policy = pol
    return pol


def _count_statuses(request: Request) -> frozenset[int]:
    raw = getattr(request.app.state, "ratelimit_count_statuses", None)
    if raw is None:
        raw = frozenset(int(x) for x in get_settings().RATELIMIT_COUNT_STATUSES.split(",")
                        if x.strip())
        request.app.state.ratelimit_count_statuses = raw
    return raw


async def optional_principal(request: Request) -> "Principal | None":
    """Resolve the caller from the bearer token — or None. NEVER 401s.

    The limiter must NOT read `request.state.principal`. That attribute is set by the auth
    dependency, and FastAPI runs a route's decorator dependencies (where `rate_limit` usually
    lives) BEFORE its parameter dependencies (where auth usually lives) — so the limiter would see
    `None`, the subject would silently drop out of the key, and everyone behind one office NAT would
    share an IP bucket, with no symptom until a customer complains. Ordering you don't control is a
    bug you can't see.

    By taking THIS as its own dependency, the limiter forces FastAPI to resolve the subject before
    the limiter body runs, whatever order the route lists things in. The trap is made
    unrepresentable, not merely avoided.

    Optional because the limiter also guards UNAUTHENTICATED routes (`/login`): a missing or invalid
    token is a legitimate `None` that falls back to IP-keying — a deliberate fallback, never an
    accidental one.
    """
    auth = request.headers.get("authorization", "")
    scheme, _, token = auth.partition(" ")
    if scheme.lower() != "bearer" or not token:
        return None
    try:
        claims = request.app.state.container.token_verifier.verify(token)
    except Exception:
        return None
    return _extract_principal(claims)


async def rate_limit(request: Request, response: Response,
                     principal: "Principal | None" = Depends(optional_principal)) -> None:
    limiter = request.app.state.container.rate_limiter
    policy = _policy(request)
    cls = policy.classify(request.url.path)
    subject = getattr(principal, "subject", None) if principal else None
    tenant = getattr(principal, "tenant", None) if principal else None
    ip = request.client.host if request.client else None
    key = policy.key(cls, subject=subject, ip=ip, tenant=tenant)

    try:
        v = await limiter.check(key, cost=cls.cost, limit=cls.limit, window_s=cls.window_s)
    except Exception:
        # Fail closed on a limiter the driver could not even TRY (a misconfig, not an outage — the
        # redis driver handles its own outage by degrading; anything reaching here is unexpected).
        # A limiter that cannot decide must not wave the request through. CWE-703.
        raise HTTPException(status.HTTP_429_TOO_MANY_REQUESTS, "RATE_LIMITED",
                            headers={"Retry-After": str(cls.window_s)})

    # These go on EVERY response, allowed or not, so a client can slow down before it is throttled.
    response.headers["RateLimit-Limit"] = str(v.limit)
    response.headers["RateLimit-Remaining"] = str(v.remaining)
    response.headers["RateLimit-Reset"] = str(v.reset_s)
    if v.degraded:
        # Honest: the shared counter was unreachable and this was answered by the per-pod fallback.
        # An operator watching for this header knows the limit is currently coarser than configured.
        response.headers["RateLimit-Degraded"] = "1"

    if not v.allowed:
        raise HTTPException(status.HTTP_429_TOO_MANY_REQUESTS, "RATE_LIMITED",
                            headers={"Retry-After": str(v.retry_after_s),
                                     "RateLimit-Limit": str(v.limit),
                                     "RateLimit-Remaining": "0",
                                     "RateLimit-Reset": str(v.reset_s)})

    # T6 (ammar's insight): on a COUNTED surface, only genuine failures should spend the budget. We
    # cannot know the status yet — the handler has not run — so we count now and let the middleware
    # (register_ratelimit_refund) REFUND this key if the final status is not one that counts. That
    # is how a correct password on an account pending approval (a 403, not a 401) avoids locking the
    # user out while they wait for the approval that would admit them.
    counted = _count_statuses(request)
    if counted:
        request.state.ratelimit_refund = (key, cls.cost, cls.limit, cls.window_s, counted)


def register_ratelimit_refund(app) -> None:
    """Refund a counted-surface request whose status did not count. Mounted once, at app build."""
    @app.middleware("http")
    async def _refund(request: Request, call_next):
        response = await call_next(request)
        pending = getattr(request.state, "ratelimit_refund", None)
        if pending is not None:
            key, cost, limit, window_s, counted = pending
            if response.status_code not in counted:
                limiter = request.app.state.container.rate_limiter
                await limiter.refund(key, cost=cost, limit=limit, window_s=window_s)
        return response
