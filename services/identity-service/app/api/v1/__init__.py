# =============================================================================
# identity-service — user tier (/api/v1)
# =============================================================================
from __future__ import annotations

from fastapi import APIRouter

from app.api.v1.auth import router as auth_router
from app.api.v1.tenancy import router as tenancy_router  # noqa: F401 — mounted by create_app

router = APIRouter()
router.include_router(auth_router)

# NOTE: tenancy_router is deliberately NOT mounted here. Multi-tenant membership (Phase 31) is
# opt-in, and the decision belongs to create_app — which runs per app — not to module import,
# which runs once and would freeze the answer before a deployment (or a test) ever set the knob.
