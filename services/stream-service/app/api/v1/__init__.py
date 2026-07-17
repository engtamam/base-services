# =============================================================================
# stream-service — User tier (/api/v1/*, token-authenticated)
# -----------------------------------------------------------------------------
# Aggregates the user-tier routers. Every route is deny-by-default: require_user
# verifies the Bearer token and a capability gate adds RBAC.
# =============================================================================
from __future__ import annotations

from fastapi import APIRouter, Depends

from app.api.auth import Principal, require_user
from app.api.authz import require_capability
from app.api.deps import get_guard
from app.api.ratelimit import rate_limit
from app.api.v1.stream import router as stream_router

router = APIRouter()
router.include_router(stream_router)

# Generic identity/RBAC probes (auth machinery, not business logic). Kept so the token-verification,
# capability-gating, and rate-limit behaviours are exercised independently of stream semantics.
generic = APIRouter(prefix="/api/v1", tags=["v1"])


@generic.get("/whoami", dependencies=[Depends(rate_limit)])
async def whoami(principal: Principal = Depends(require_user), guard=Depends(get_guard)) -> dict:
    async with guard.step("respond") as step:
        body = {"subject": principal.subject, "caps": sorted(principal.caps),
                "tenant": principal.tenant}
        step.result = body
    return body


@generic.get("/admin/ping", dependencies=[Depends(require_capability("admin"))])
async def admin_ping() -> dict:
    return {"status": "ok"}


router.include_router(generic)
