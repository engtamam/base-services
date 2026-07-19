# =============================================================================
# verification-service — User tier (/api/v1/*, token-authenticated)
# -----------------------------------------------------------------------------
# Every route is protected: Depends(require_user) verifies the Bearer token
# (deny-by-default), require_capability adds fine-grained RBAC, rate_limit
# throttles, and guard.step wraps risky work with CWE pre/post checks.
# The routes below are EXAMPLES — replace with real business routes per service.
# =============================================================================
from __future__ import annotations

from fastapi import APIRouter, Depends

from app.api.auth import Principal, require_user
from app.api.authz import require_capability
from app.api.deps import get_guard
from app.api.ratelimit import rate_limit

router = APIRouter(prefix="/api/v1", tags=["v1"])


@router.get("/whoami", dependencies=[Depends(rate_limit)])
async def whoami(
    principal: Principal = Depends(require_user),
    guard=Depends(get_guard),
) -> dict:
    # Demonstrate the self-guard: the response is checked for secret leakage
    # (CWE-200/532) before it leaves the service.
    async with guard.step("respond") as step:
        body = {
            "subject": principal.subject,
            "caps": sorted(principal.caps),
            "tenant": principal.tenant,
        }
        step.result = body
    return body


@router.get("/admin/ping", dependencies=[Depends(require_capability("admin"))])
async def admin_ping() -> dict:
    # Reached only by a token whose `caps` include "admin" (deny-by-default).
    return {"status": "ok"}

# TODO (per service): add real business routes here.


# The real surface. /whoami and /admin/ping above stay as the template's probes.
from app.api.v1.verification import router as _verification_router  # noqa: E402

router.include_router(_verification_router)
