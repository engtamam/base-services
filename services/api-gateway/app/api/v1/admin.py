"""GET /api/v1/admin/catalog — the platform admin panel's one call.

USER-tier: the panel is a browser app, so it authenticates with the operator's own JWT (which
carries their caps), NOT the internal service token. The aggregator holds the internal token
server-side to poll each service (phase-33 T3: a token that reaches the browser reaches everyone).

The response is only the actions THIS caller may perform (filtered by their caps), across every
service in ADMIN_SERVICES — ours and a customer's own product service alike.
"""
from __future__ import annotations

import time

import httpx
from app.core.internal_client import assertion_headers_for_url
from fastapi import APIRouter, Depends, Request

from app.api.auth import Principal, require_user
from app.api.authz import require_capability
from app.config import get_settings
from app.core.admin_catalog import ServiceTarget, build_catalog, parse_targets

router = APIRouter(prefix="/api/v1/admin", tags=["admin"])

# A tiny per-process cache: (expires_at, caller_caps_key) -> catalog. Keyed by caps because the
# catalog is filtered per caller; the underlying manifests are the same, but the visible slice is not.
_CACHE: dict[str, tuple[float, dict]] = {}


def _cap_gate():
    return require_capability(get_settings().ADMIN_CATALOG_CAP)


@router.get("/catalog")
async def catalog(request: Request, principal: Principal = Depends(require_user)) -> dict:
    """Every admin action this operator may perform, grouped by service.

    Gated by ADMIN_CATALOG_CAP: opening the panel at all requires a capability, so the catalog
    cannot be used to enumerate the platform's controls anonymously (T5). Each action is then
    filtered again by whether the caller holds ITS specific capability.
    """
    # Enforce the panel-open capability explicitly (kept as a call so the cap name is config, not a
    # decorator constant).
    await _cap_gate()(principal)

    s = get_settings()
    targets = parse_targets(s.ADMIN_SERVICES)
    caps = principal.caps

    key = "|".join(sorted(caps))
    now = time.monotonic()
    hit = _CACHE.get(key)
    if hit and hit[0] > now:
        return hit[1]

    token = s.INTERNAL_SERVICE_TOKEN
    timeout = s.UPSTREAM_TIMEOUT_SECONDS

    async def fetch(t: ServiceTarget) -> dict | None:
        # The internal token rides here, server-side only. The URL is the CONFIGURED base_url, so a
        # declaration can never redirect this call (T2/T4).
        async with httpx.AsyncClient(timeout=timeout) as client:
            r = await client.get(f"{t.base_url}/internal/manifest",
                                 headers={"X-Internal-Token": token,
                                          **assertion_headers_for_url(t.base_url)})
        return r.json() if r.status_code == 200 else None

    result = await build_catalog(targets, caps, fetch)
    if s.ADMIN_CATALOG_TTL > 0:
        _CACHE[key] = (now + s.ADMIN_CATALOG_TTL, result)
    return result
