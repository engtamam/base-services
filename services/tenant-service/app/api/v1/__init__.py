# =============================================================================
# tenant-service — user tier (/api/v1)
# =============================================================================
from __future__ import annotations

from fastapi import APIRouter

from app.api.v1.tenants import admin_router, self_router

router = APIRouter()
router.include_router(admin_router)
router.include_router(self_router)
