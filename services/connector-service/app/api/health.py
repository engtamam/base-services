# =============================================================================
# connector-service — Health & readiness (public, no auth)
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


async def _run_check(name: str, check: ReadinessCheck, timeout: float) -> tuple[str, str, bool]:
    """Run ONE readiness probe under its own bounded timeout. Returns (name, state, ok). Never raises:
    a failing or hanging probe becomes a state ("down"/"timeout"), never a 500 on the endpoint."""
    try:
        res = await asyncio.wait_for(check(), timeout)
        # A check may return a bool (ok/down) or a REASON string ("ok", "pool exhausted …", "down").
        # A string is reported verbatim so readiness can name WHY, not just that it is not ready.
        if isinstance(res, str):
            return name, res, (res == "ok")
        ok = bool(res)
        return name, "ok" if ok else "down", ok
    except asyncio.TimeoutError:
        # DISTINCT from "down": the check did not answer within the budget — almost always a
        # dependency unreachable in a way that hangs rather than refuses.
        return name, "timeout", False
    except Exception:  # a failing probe must never crash the endpoint
        return name, "down", False


@router.get("/readyz")
async def readyz() -> JSONResponse:
    # Every dependency probe runs CONCURRENTLY, each under its OWN bounded timeout. Sequentially the
    # worst case was the SUM of every check's timeout (6 deps x 5s = 30s), which blows past the
    # kubelet's own httpGet timeout (5s) on a busy node — the pod never turns Ready even though every
    # dependency is actually fine, only slow. Concurrent, the worst case is the SLOWEST SINGLE check.
    # A bounded per-check wait still turns a blackholed dependency into `{"<dep>": "timeout"}` (a named
    # reason) rather than a silent hang with no response body.
    timeout = getattr(get_settings(), "READYZ_TIMEOUT_SECONDS", _DEFAULT_READYZ_TIMEOUT)
    checks = list(_readiness_checks.items())
    gathered = await asyncio.gather(*(_run_check(n, c, timeout) for n, c in checks))
    results = {name: state for name, state, _ in gathered}
    all_ok = all(ok for _, _, ok in gathered)
    status_code = 200 if all_ok else 503
    return JSONResponse(
        {"status": "ready" if all_ok else "not_ready", "checks": results},
        status_code=status_code,
    )
