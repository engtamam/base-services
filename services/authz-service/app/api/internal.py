# =============================================================================
# authz-service — Internal tier (service-to-service, X-Internal-Token)
# -----------------------------------------------------------------------------
# NOT user-facing. Guarded by a shared token compared in CONSTANT TIME
# (CWE-208: observable timing discrepancy). Never a user JWT.
# =============================================================================
from __future__ import annotations

import hmac

from fastapi import APIRouter, Depends, Header, HTTPException, Query, Request, status
from pydantic import BaseModel, Field

from app.api.v1.authz import build_authz
from app.config import Settings, get_settings
from app.manifest import build_manifest

router = APIRouter(prefix="/internal", tags=["internal"])


class CheckIn(BaseModel):
    subject: str = Field(default="", max_length=256)
    action: str = Field(min_length=1, max_length=256)
    resource: str = Field(default="", max_length=512)
    tenant: str = Field(default="", max_length=128)
    extra_caps: list[str] = Field(default_factory=list)
    attributes: dict = Field(default_factory=dict)

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


@router.post("/authz/check", dependencies=[Depends(require_internal_token)])
async def internal_check(request: Request, body: CheckIn) -> dict:
    """PDP — can `subject` do `action` on `resource`? Returns {allow, reason, matched_policy}."""
    decision = await build_authz(request).check(
        subject=body.subject, action=body.action, resource=body.resource, tenant=body.tenant,
        extra_caps=body.extra_caps, attributes=body.attributes)
    return decision.to_dict()


@router.get("/authz/effective", dependencies=[Depends(require_internal_token)])
async def internal_effective(request: Request, subject: str = Query(...),
                             tenant: str = Query(default="")) -> dict:
    """Resolve a subject's effective capabilities (for identity to embed in tokens)."""
    caps = await build_authz(request).effective(subject, tenant)
    return {"subject": subject, "tenant": tenant, "capabilities": sorted(caps)}


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
