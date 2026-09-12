# =============================================================================
# api-gateway — Prometheus metrics (internal tier only)
# -----------------------------------------------------------------------------
# Exposes the default process/registry metrics in Prometheus text format. Guarded by the internal
# token: the RED metrics are grouped by route template and leak the surface + traffic shape, so the
# scraper must present X-Internal-Token — not the public internet (api-gateway-r2-09).
# =============================================================================
from __future__ import annotations

import hmac

from fastapi import APIRouter, Depends, Header, HTTPException, status
from prometheus_client import CONTENT_TYPE_LATEST, generate_latest
from starlette.responses import Response

from app.config import Settings, get_settings

router = APIRouter(tags=["metrics"])


async def _require_internal_token(
    x_internal_token: str | None = Header(default=None, alias="X-Internal-Token"),
    settings: Settings = Depends(get_settings),
) -> None:
    expected = settings.INTERNAL_SERVICE_TOKEN
    if not (x_internal_token and hmac.compare_digest(x_internal_token, expected)):
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "UNAUTHORIZED")


@router.get("/metrics", dependencies=[Depends(_require_internal_token)])
async def metrics() -> Response:
    return Response(content=generate_latest(), media_type=CONTENT_TYPE_LATEST)
