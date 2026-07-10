# =============================================================================
# compliance-service — user tier (/api/v1)
# =============================================================================
from __future__ import annotations

from fastapi import APIRouter

from app.api.v1.compliance import router as compliance_router

router = APIRouter()
router.include_router(compliance_router)
