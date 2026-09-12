# =============================================================================
# api-gateway — the reverse-proxy catch-all (mounted LAST)
# -----------------------------------------------------------------------------
# Any path not served by the gateway's own tiers (/healthz, /metrics,
# /internal/*) is matched against the route table and forwarded upstream.
# =============================================================================
from __future__ import annotations

from fastapi import APIRouter, Request
from starlette.responses import Response

from app.config import get_settings
from app.core.gateway import Gateway

router = APIRouter()

_METHODS = ["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS", "HEAD"]


def build_gateway(request: Request) -> Gateway:
    c = request.app.state.container
    s = get_settings()
    # The policy-driven limiter (per-route class/limit/cost, composite key) is built once on app.state;
    # the proxy hot path must apply it, not a flat per-IP default. Fall back to building it if absent.
    policy = getattr(request.app.state, "ratelimit_policy", None)
    if policy is None:
        from app.api.ratelimit import build_policy
        policy = build_policy(s)
        request.app.state.ratelimit_policy = policy
    return Gateway(
        route_table=c.route_table, forwarder=c.forwarder, token_verifier=c.token_verifier,
        rate_limiter=c.rate_limiter, cache=c.cache,
        internal_token=s.INTERNAL_SERVICE_TOKEN, default_cache_ttl=s.DEFAULT_CACHE_TTL,
        policy=policy, trusted_proxy_hops=s.GATEWAY_TRUSTED_PROXY_HOPS,
    )


# Excluded from the OpenAPI schema on purpose. This is ONE handler bound to several methods, so
# FastAPI derives the same operationId for each and emits duplicates — which breaks client
# generators outright and, because the collision resolves in arbitrary order, made the generated
# spec differ between two identical runs.
#
# It is also not an API worth documenting: the gateway PROXIES to the services behind it, and each
# of those publishes its own accurate spec. Describing a catch-all `/{full_path}` here would tell a
# customer nothing except that any path is accepted.
@router.api_route("/{full_path:path}", methods=_METHODS, include_in_schema=False)
async def proxy(request: Request, full_path: str) -> Response:
    return await build_gateway(request).handle(request)
