# =============================================================================
# identity-service — user tier (/api/v1)
# =============================================================================
from __future__ import annotations

from fastapi import APIRouter

from app.api.v1.auth import router as auth_router

router = APIRouter()
router.include_router(auth_router)
