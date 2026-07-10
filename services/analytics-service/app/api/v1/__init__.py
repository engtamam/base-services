# =============================================================================
# analytics-service — User tier (/api/v1/*, token-authenticated)
# -----------------------------------------------------------------------------
# Every route is protected: Depends(require_user) verifies the Bearer token
# (deny-by-default), require_*_capability adds fine-grained RBAC, rate_limit
# throttles.
# =============================================================================
from __future__ import annotations

from fastapi import APIRouter

from app.api.v1.analytics import router as analytics_router

router = APIRouter()
router.include_router(analytics_router)
