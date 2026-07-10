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
    return Gateway(
        route_table=c.route_table, forwarder=c.forwarder, token_verifier=c.token_verifier,
        rate_limiter=c.rate_limiter, cache=c.cache,
        internal_token=s.INTERNAL_SERVICE_TOKEN, default_cache_ttl=s.DEFAULT_CACHE_TTL,
    )


@router.api_route("/{full_path:path}", methods=_METHODS)
async def proxy(request: Request, full_path: str) -> Response:
    return await build_gateway(request).handle(request)
