# =============================================================================
# connector-service — user tier (/api/v1)
# =============================================================================
from __future__ import annotations

from fastapi import APIRouter

from app.api.v1.connectors import router as connectors_router
from app.api.v1.operator import router as operator_router

router = APIRouter()
router.include_router(connectors_router)
router.include_router(operator_router)
