# =============================================================================
# authz-service — user tier (/api/v1)
# =============================================================================
from __future__ import annotations

from fastapi import APIRouter

from app.api.v1.authz import router as authz_router

router = APIRouter()
router.include_router(authz_router)
