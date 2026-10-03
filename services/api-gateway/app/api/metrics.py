# =============================================================================
# api-gateway — Prometheus metrics (internal tier only)
# -----------------------------------------------------------------------------
# Exposes the default process/registry metrics in Prometheus text format. Guarded by the internal
# token: the RED metrics are grouped by route template and leak the surface + traffic shape, so the
# scraper must present X-Internal-Token — not the public internet (api-gateway-r2-09).
# =============================================================================
from __future__ import annotations

from fastapi import APIRouter, Depends
from prometheus_client import CONTENT_TYPE_LATEST, generate_latest
from starlette.responses import Response

# Guard /metrics with the SAME internal-caller resolution + route policy as the /internal tier, not a
# bespoke flat-token compare — so its auth strength tracks the fleet's configuration (assertion identity
# and per-route policy when enabled), while the flat shared token keeps working (api-gateway-013).
from app.api.internal import require_internal_token

router = APIRouter(tags=["metrics"])


@router.get("/metrics", dependencies=[Depends(require_internal_token)])
async def metrics() -> Response:
    return Response(content=generate_latest(), media_type=CONTENT_TYPE_LATEST)
