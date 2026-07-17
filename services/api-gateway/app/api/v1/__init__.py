# =============================================================================
# api-gateway — no own /api/v1 business routes; the proxy (app/api/proxy.py)
# forwards everything to upstreams. This router stays empty on purpose.
# =============================================================================
from __future__ import annotations

from fastapi import APIRouter

from app.api.v1.admin import router as admin_router
from app.api.v1.app_catalog import router as app_router

router = APIRouter()
# The platform admin panel's aggregator (Phase 33) and the end-user app aggregator (Phase 36). The
# gateway is the natural host for both: it already fronts every service and holds the internal token,
# so the browser talks only to it. Admin reads `admin_actions`; app reads `user_actions`.
router.include_router(admin_router)
router.include_router(app_router)
