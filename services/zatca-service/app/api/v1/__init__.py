# =============================================================================
# zatca-service — user tier (/api/v1)
# =============================================================================
from __future__ import annotations

from fastapi import APIRouter

from app.api.v1.zatca import router as zatca_router

router = APIRouter()
router.include_router(zatca_router)
