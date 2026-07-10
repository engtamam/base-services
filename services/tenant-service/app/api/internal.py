# =============================================================================
# tenant-service — Internal tier (service-to-service, X-Internal-Token)
# -----------------------------------------------------------------------------
# NOT user-facing. Guarded by a shared token compared in CONSTANT TIME
# (CWE-208: observable timing discrepancy). Never a user JWT.
# =============================================================================
from __future__ import annotations

import hmac

from fastapi import APIRouter, Depends, Header, HTTPException, Query, Request, status

from app.api.v1.tenants import build_service
from app.config import Settings, get_settings
from app.manifest import build_manifest

router = APIRouter(prefix="/internal", tags=["internal"])

_HEADER = "X-Internal-Token"


async def require_internal_token(
    x_internal_token: str | None = Header(default=None, alias=_HEADER),
    settings: Settings = Depends(get_settings),
) -> None:
    """Deny unless the caller presents the exact internal token (timing-safe)."""
    expected = settings.INTERNAL_SERVICE_TOKEN
    provided = x_internal_token or ""
    # hmac.compare_digest is constant-time and length-safe.
    if not hmac.compare_digest(provided, expected):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED, detail="UNAUTHORIZED"
        )


@router.get("/health", dependencies=[Depends(require_internal_token)])
async def internal_health(settings: Settings = Depends(get_settings)) -> dict[str, str]:
    return {"status": "ok", "service": settings.SERVICE_NAME}


@router.get("/tenants/resolve", dependencies=[Depends(require_internal_token)])
async def internal_resolve(request: Request, host: str = Query(..., max_length=253)) -> dict:
    """host -> tenant (for peers routing by domain). 404 if unresolved."""
    tenant = await build_service(request).resolve_by_host(host)
    if tenant is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "NOT_FOUND")
    return {"id": tenant.id, "slug": tenant.slug, "status": tenant.status,
            "isolation": build_service(request).scope(tenant), "config": tenant.config}


@router.get("/tenants/{tenant_id}", dependencies=[Depends(require_internal_token)])
async def internal_get(request: Request, tenant_id: str) -> dict:
    """Tenant config + scope for peers (data scoping)."""
    svc = build_service(request)
    tenant = await svc.get(tenant_id)
    if tenant is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "NOT_FOUND")
    return {"id": tenant.id, "slug": tenant.slug, "status": tenant.status,
            "isolation": svc.scope(tenant), "config": tenant.config}


@router.get("/manifest", dependencies=[Depends(require_internal_token)])
async def internal_manifest(
    request: Request, settings: Settings = Depends(get_settings)
) -> dict:
    container = getattr(request.app.state, "container", None)
    ports = container.describe() if container is not None else []
    manifest = build_manifest(settings, ports=ports)
    guard = getattr(request.app.state, "cwe_guard", None)
    if guard is not None:
        manifest["security"] = {"cwe_guard": guard.report()}
    return manifest
