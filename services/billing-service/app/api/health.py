# =============================================================================
# billing-service — Health & readiness (public, no auth)
# -----------------------------------------------------------------------------
#   /healthz — liveness: is the process up? Always cheap, never touches deps.
#   /readyz  — readiness: are dependencies reachable? Phase 1 has none, so it is
#              ready. A registry of async checks is provided for later phases to
#              append dependency probes (DataStore, Cache, ...) without edits here.
# =============================================================================
from __future__ import annotations

from collections.abc import Awaitable, Callable

from fastapi import APIRouter
from starlette.responses import JSONResponse

from app.config import get_settings

router = APIRouter(tags=["health"])

# Readiness probe registry. name -> async () -> bool. Later phases append checks.
ReadinessCheck = Callable[[], Awaitable[bool]]
_readiness_checks: dict[str, ReadinessCheck] = {}


def register_readiness_check(name: str, check: ReadinessCheck) -> None:
    _readiness_checks[name] = check


@router.get("/healthz")
async def healthz() -> dict[str, str]:
    return {"status": "ok", "service": get_settings().SERVICE_NAME}


@router.get("/readyz")
async def readyz() -> JSONResponse:
    results: dict[str, str] = {}
    all_ok = True
    for name, check in _readiness_checks.items():
        try:
            ok = await check()
        except Exception:  # a failing probe must never crash the endpoint
            ok = False
        results[name] = "ok" if ok else "down"
        all_ok = all_ok and ok
    status_code = 200 if all_ok else 503
    return JSONResponse(
        {"status": "ready" if all_ok else "not_ready", "checks": results},
        status_code=status_code,
    )
