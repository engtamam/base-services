# =============================================================================
# audit-service — Internal tier (service-to-service, X-Internal-Token)
# -----------------------------------------------------------------------------
# NOT user-facing. Guarded by a shared token compared in CONSTANT TIME
# (CWE-208: observable timing discrepancy). Never a user JWT.
# =============================================================================
from __future__ import annotations

import hmac

from fastapi import APIRouter, Depends, Header, HTTPException, Request, status
from pydantic import BaseModel, Field

from app.api.v1.audit import build_audit
from app.config import Settings, get_settings
from app.core.erasure import AuditErasure
from app.manifest import build_manifest

router = APIRouter(prefix="/internal", tags=["internal"])


class AuditIn(BaseModel):
    service: str = Field(min_length=1, max_length=128)
    action: str = Field(min_length=1, max_length=128)
    actor: str = Field(default="", max_length=256)
    resource: str = Field(default="", max_length=512)
    tenant: str = Field(default="", max_length=128)
    ip: str = Field(default="", max_length=64)
    metadata: dict = Field(default_factory=dict)

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


@router.post("/audit/enforce-retention", dependencies=[Depends(require_internal_token)])
async def enforce_retention(request: Request, settings: Settings = Depends(get_settings)) -> dict:
    """Apply the RETENTION_DAYS policy (a scheduler calls this). 0 = keep forever."""
    # Write-ahead: a critical action the log cannot take must not happen at all, so this
    # runs BEFORE the mutation. Recording afterwards would 503 a caller whose action had
    # already succeeded, and leave it unrecorded.
    await request.app.state.auditor.record(
        "audit.retention_enforced", actor="system:cron", tenant="", resource="audit_log",
        ip=request.client.host if request.client else "")
    # Hold protection, when a driver is configured. Retention used to delete every expired event,
    # including events belonging to a subject under legal hold — this service could not ask,
    # because its rows carry a pseudonym rather than a subject. The refs come per partition and
    # the intersection happens locally; an incomplete answer skips that partition rather than
    # purging blind. `hold_protected` in the response says which of those two happened.
    gate = request.app.state.container.hold_refs
    holds = gate.for_tenant if gate is not None and gate.driver != "null" else None
    return await build_audit(request).enforce_retention(settings.RETENTION_DAYS, holds=holds)


@router.get("/health", dependencies=[Depends(require_internal_token)])
async def internal_health(settings: Settings = Depends(get_settings)) -> dict[str, str]:
    return {"status": "ok", "service": settings.SERVICE_NAME}


@router.post("/audit", dependencies=[Depends(require_internal_token)])
async def internal_audit(request: Request, body: AuditIn) -> dict:
    """Primary ingest — a service records an audit event here (service-to-service)."""
    evt = await build_audit(request).record(
        service=body.service, action=body.action, actor=body.actor,
        resource=body.resource, tenant=body.tenant, ip=body.ip, metadata=body.metadata,
    )
    return {"id": evt.id, "seq": evt.seq, "hash": evt.hash}


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
    """Audit erases NOTHING — and says so, with a basis, and proves the chain still verifies.

    The events carry a pseudonym, not an identity. Once identity-service deletes the linkage they
    name nobody. Redacting them instead would break the hash chain from that seq onward, which is
    the one thing this service exists to make impossible.
    """
    # Write-ahead: a critical action the log cannot take must not happen at all, so this
    # runs BEFORE the mutation. Recording afterwards would 503 a caller whose action had
    # already succeeded, and leave it unrecorded.
    await request.app.state.auditor.record(
        "subject.erased", actor="", tenant=body.tenant, resource=f"subject:{body.subject}",
        ip=request.client.host if request.client else "")
    return await AuditErasure(audit=build_audit(request)).erase(body.tenant, body.subject)
