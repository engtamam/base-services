# =============================================================================
# api-gateway — no own /api/v1 business routes; the proxy (app/api/proxy.py)
# forwards everything to upstreams. This router stays empty on purpose.
# =============================================================================
from __future__ import annotations

from fastapi import APIRouter

from app.api.v1.admin import router as admin_router

router = APIRouter()
# The platform admin panel's aggregator (Phase 33). The gateway is the natural host: it already
# fronts every service and holds the internal token, so the browser talks only to it.
router.include_router(admin_router)
