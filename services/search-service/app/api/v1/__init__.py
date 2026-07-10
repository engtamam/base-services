# =============================================================================
# search-service — user tier (/api/v1)
# =============================================================================
from __future__ import annotations

from fastapi import APIRouter

from app.api.v1.search import router as search_router

router = APIRouter()
router.include_router(search_router)
