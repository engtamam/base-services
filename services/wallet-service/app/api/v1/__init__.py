# =============================================================================
# wallet-service — User tier (/api/v1/*, token-authenticated, read-only)
# =============================================================================
from __future__ import annotations

from fastapi import APIRouter

from app.api.v1.wallet import router as wallet_router

router = APIRouter()
router.include_router(wallet_router)
