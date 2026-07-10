# =============================================================================
# billing-service — user tier (/api/v1)
# =============================================================================
from __future__ import annotations

from fastapi import APIRouter

from app.api.v1.billing import router as billing_router

router = APIRouter()
router.include_router(billing_router)
