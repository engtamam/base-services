# =============================================================================
# notification-service — user tier (/api/v1)
# =============================================================================
from __future__ import annotations

from fastapi import APIRouter

from app.api.v1.notifications import (notifications_router, templates_router, wa_flows_router,
                                     wa_templates_router)

router = APIRouter()
router.include_router(notifications_router)
router.include_router(templates_router)
router.include_router(wa_templates_router)
router.include_router(wa_flows_router)
