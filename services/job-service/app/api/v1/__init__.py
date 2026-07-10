# =============================================================================
# job-service — user tier (/api/v1)
# =============================================================================
from __future__ import annotations

from fastapi import APIRouter

from app.api.v1.jobs import router as jobs_router

router = APIRouter()
router.include_router(jobs_router)
