# =============================================================================
# feature-flag-service — user tier (/api/v1)
# =============================================================================
from __future__ import annotations

from fastapi import APIRouter

from app.api.v1.flags import router as flags_router

router = APIRouter()
router.include_router(flags_router)
