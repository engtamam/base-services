"""GET /api/v1/app/catalog — the end-user app's one call (the user-facing twin of the admin panel).

Same aggregator, same anti-SSRF and capability-filtering rules as /api/v1/admin/catalog; two
differences:

  * it reads each service's `user_actions` (self-service routes), not `admin_actions`, and
  * there is NO "open" capability — any authenticated user may fetch their OWN app catalog. It is
    still filtered to the actions the caller's capabilities allow, so a user only ever sees what they
    could actually do; and every action is re-checked AND re-scoped to the caller's tenant/identity
    at its owning route (the catalog is a map, never the authority).

Discovery reuses ADMIN_SERVICES (the same service list feeds both panels); the internal token stays
server-side, and the result is cached per caller-caps like the admin catalog.
"""
from __future__ import annotations

import time

import httpx
from fastapi import APIRouter, Depends, Request

from app.api.auth import Principal, require_user
from app.config import get_settings
from app.core.admin_catalog import ServiceTarget, build_catalog, parse_targets

router = APIRouter(prefix="/api/v1/app", tags=["app"])

# Per-process cache keyed by caller caps (the visible slice differs per caller; the manifests do not).
_CACHE: dict[str, tuple[float, dict]] = {}


@router.get("/catalog")
async def catalog(request: Request, principal: Principal = Depends(require_user)) -> dict:
    """Every self-service action THIS user may perform, grouped by service."""
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
        # Internal token server-side only; the URL is the CONFIGURED base_url (no declaration can
        # redirect it).
        async with httpx.AsyncClient(timeout=timeout) as client:
            r = await client.get(f"{t.base_url}/internal/manifest",
                                 headers={"X-Internal-Token": token})
        return r.json() if r.status_code == 200 else None

    result = await build_catalog(targets, caps, fetch, field="user_actions")
    if s.ADMIN_CATALOG_TTL > 0:
        _CACHE[key] = (now + s.ADMIN_CATALOG_TTL, result)
    return result
