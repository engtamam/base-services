# =============================================================================
# webhook-service — user tier (/api/v1)
# =============================================================================
from __future__ import annotations

from fastapi import APIRouter

from app.api.v1.webhooks import router as webhooks_router

router = APIRouter()
router.include_router(webhooks_router)
