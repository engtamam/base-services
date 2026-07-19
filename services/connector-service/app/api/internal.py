# =============================================================================
# connector-service — Internal tier (service-to-service, X-Internal-Token)
# -----------------------------------------------------------------------------
# NOT user-facing. Guarded by a shared token compared in CONSTANT TIME
# (CWE-208: observable timing discrepancy). Never a user JWT.
# =============================================================================
from __future__ import annotations

import hmac

from typing import Any

from fastapi import APIRouter, Depends, Header, HTTPException, Request, status
from pydantic import BaseModel, Field

from app.api.v1.connectors import _spec_for, build_sync
from app.config import Settings, get_settings
from app.core.erasure import ConnectorErasure
from app.core.stores import ConnectionStore
from app.manifest import build_manifest

router = APIRouter(prefix="/internal", tags=["internal"])


class SyncIn(BaseModel):
    connection_id: str
    resource: str = Field(min_length=1, max_length=128)
    direction: str = Field(default="pull", pattern="^(pull|push)$")
    records: list[dict[str, Any]] = Field(default_factory=list)

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


@router.post("/connectors/sync", dependencies=[Depends(require_internal_token)])
async def internal_sync(request: Request, body: SyncIn) -> dict:
    """Run one sync batch. job-service schedules and retries this (durable, DLQ-backed)."""
    conn = await ConnectionStore(request.app.state.container.datastore).get(body.connection_id)
    if conn is None:
        raise HTTPException(404, "NOT_FOUND")
    spec = await _spec_for(request, conn)
    engine = build_sync(request)
    field_map = spec.get("field_map") or {}
    if body.direction == "pull":
        return await engine.pull(connection_id=conn.id, resource=body.resource, spec=spec,
                                 field_map=field_map)
    return await engine.push(connection_id=conn.id, resource=body.resource,
                             records=body.records, spec=spec, field_map=field_map)


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


class EraseIn(BaseModel):
    tenant: str
    subject: str


@router.post("/privacy/erase", dependencies=[Depends(require_internal_token)])
async def erase_subject(request: Request, body: EraseIn) -> dict:
    """Remove the data subject from this service's untyped payloads."""
    # Write-ahead: a critical action the log cannot take must not happen at all, so this
    # runs BEFORE the mutation. Recording afterwards would 503 a caller whose action had
    # already succeeded, and leave it unrecorded.
    await request.app.state.auditor.record(
        "subject.erased", actor="", tenant=body.tenant, resource=f"subject:{body.subject}",
        ip=request.client.host if request.client else "")
    c = request.app.state.container
    s = get_settings()
    eraser = ConnectorErasure(store=c.datastore, pii_salt=s.PII_SALT, text_keys=s.erasure_text_keys,
                  max_scan=s.ERASURE_MAX_SCAN)
    return await eraser.erase(body.tenant, body.subject)
