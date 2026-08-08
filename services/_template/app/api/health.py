# =============================================================================
# {service-name} — Health & readiness (public, no auth)
# -----------------------------------------------------------------------------
#   /healthz — liveness: is the process up? Always cheap, never touches deps.
#   /readyz  — readiness: are dependencies reachable? Phase 1 has none, so it is
#              ready. A registry of async checks is provided for later phases to
#              append dependency probes (DataStore, Cache, ...) without edits here.
# =============================================================================
from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable

from fastapi import APIRouter
from starlette.responses import JSONResponse

from app.config import get_settings

router = APIRouter(tags=["health"])

# Readiness probe registry. name -> async () -> bool. Later phases append checks.
ReadinessCheck = Callable[[], Awaitable[bool]]
_readiness_checks: dict[str, ReadinessCheck] = {}

# Fallback per-check timeout when the service's Settings does not set READYZ_TIMEOUT_SECONDS.
_DEFAULT_READYZ_TIMEOUT = 5.0


def register_readiness_check(name: str, check: ReadinessCheck) -> None:
    _readiness_checks[name] = check


@router.get("/healthz")
async def healthz() -> dict[str, str]:
    return {"status": "ok", "service": get_settings().SERVICE_NAME}


@router.get("/readyz")
async def readyz() -> JSONResponse:
    # Each dependency probe runs under a BOUNDED timeout. A blackholed dependency (an egress
    # NetworkPolicy that DROPS rather than refuses, a wedged upstream) would otherwise leave the
    # probe hanging until the kubelet's own deadline, with NO response body — the pod sits
    # Running 0/1 and nothing says why. A bounded wait turns that silence into
    # `{"<dep>": "timeout"}`, which points straight at the stuck dependency instead of reading as a
    # slow start, then a bad image, then a missing bucket in turn.
    timeout = getattr(get_settings(), "READYZ_TIMEOUT_SECONDS", _DEFAULT_READYZ_TIMEOUT)
    results: dict[str, str] = {}
    all_ok = True
    for name, check in _readiness_checks.items():
        try:
            res = await asyncio.wait_for(check(), timeout)
            # A check may return a bool (ok/down) or a REASON string ("ok", "pool exhausted …",
            # "down"). A string is reported verbatim so readiness can name WHY, not just that it is
            # not ready — a full datastore must not read as an unreachable one.
            if isinstance(res, str):
                state, ok = res, (res == "ok")
            else:
                ok = bool(res)
                state = "ok" if ok else "down"
        except asyncio.TimeoutError:
            # DISTINCT from "down": the check did not answer within the budget — almost always a
            # dependency that is unreachable in a way that hangs rather than refuses.
            state, ok = "timeout", False
        except Exception:  # a failing probe must never crash the endpoint
            state, ok = "down", False
        results[name] = state
        all_ok = all_ok and ok
    status_code = 200 if all_ok else 503
    return JSONResponse(
        {"status": "ready" if all_ok else "not_ready", "checks": results},
        status_code=status_code,
    )
