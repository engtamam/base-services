# =============================================================================
# audit-service — user tier (/api/v1)
# =============================================================================
from __future__ import annotations

from fastapi import APIRouter

from app.api.v1.audit import router as audit_router

router = APIRouter()
router.include_router(audit_router)
