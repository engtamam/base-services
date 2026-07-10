# =============================================================================
# webhook-service — Internal tier (service-to-service, X-Internal-Token)
# -----------------------------------------------------------------------------
# NOT user-facing. Guarded by a shared token compared in CONSTANT TIME
# (CWE-208: observable timing discrepancy). Never a user JWT.
# =============================================================================
from __future__ import annotations

import hmac

from typing import Any

from fastapi import APIRouter, Depends, Header, HTTPException, Request, status
from pydantic import BaseModel, Field

from app.api.v1.webhooks import build_webhooks
from app.config import Settings, get_settings
from app.manifest import build_manifest

router = APIRouter(prefix="/internal", tags=["internal"])


class DispatchIn(BaseModel):
    tenant: str = Field(min_length=1)
    type: str = Field(min_length=1, max_length=128)
    payload: dict[str, Any] = Field(default_factory=dict)

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


@router.post("/webhooks/dispatch", status_code=202, dependencies=[Depends(require_internal_token)])
async def dispatch(request: Request, body: DispatchIn) -> dict:
    """Any service fans an event out to that tenant's subscribers. HOT PATH (cached lookup)."""
    try:
        return await build_webhooks(request).dispatch(tenant=body.tenant, event_type=body.type,
                                                      payload=body.payload)
    except ValueError as e:
        raise HTTPException(413, str(e))


@router.post("/webhooks/deliver/{delivery_id}", dependencies=[Depends(require_internal_token)])
async def deliver(request: Request, delivery_id: str) -> dict:
    return await build_webhooks(request).deliver(delivery_id)


@router.post("/webhooks/sweep", dependencies=[Depends(require_internal_token)])
async def sweep(request: Request) -> dict:
    """Worker tick: deliver everything whose jittered backoff has elapsed."""
    return await build_webhooks(request).sweep()


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
