# =============================================================================
# analytics-service — Prometheus metrics (public, no auth)
# -----------------------------------------------------------------------------
# Exposes the default process/registry metrics in Prometheus text format.
# =============================================================================
from __future__ import annotations

from fastapi import APIRouter
from prometheus_client import CONTENT_TYPE_LATEST, generate_latest
from starlette.responses import Response

router = APIRouter(tags=["metrics"])


@router.get("/metrics")
async def metrics() -> Response:
    return Response(content=generate_latest(), media_type=CONTENT_TYPE_LATEST)
