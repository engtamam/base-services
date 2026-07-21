# =============================================================================
# audit-service — Internal tier (service-to-service, X-Internal-Token)
# -----------------------------------------------------------------------------
# NOT user-facing. Guarded by a shared token compared in CONSTANT TIME
# (CWE-208: observable timing discrepancy). Never a user JWT.
# =============================================================================
from __future__ import annotations

import hmac

from fastapi import APIRouter, Depends, Header, HTTPException, Request, status
from pydantic import BaseModel, Field, model_validator

from app.api.v1.audit import build_audit
from app.config import Settings, get_settings
from app.core.erasure import AuditErasure
from app.core.pii import subject_ref
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


@router.post("/audit/verify", dependencies=[Depends(require_internal_token)])
async def verify_all(request: Request) -> dict:
    """Verify EVERY partition's hash chain (a scheduler calls this on a timer).

    The chain is tamper-evident but only proves anything when someone walks it. `/api/v1/audit/
    verify` is user-tier (`platform.audit`), so an internal-token cron cannot reach it, and with
    AUDIT_PARTITION=tenant only this service knows the partitions to check — so the scheduled
    verification has to live here, behind the internal token, enumerating partitions itself.

    FAILS LOUD. A broken chain returns 409, not a 200 with ok:false, so the cronJob's request
    fails, the Job is marked failed, and the alert fires. A silent 200 would make the schedule a
    formality — the exact "verifiable but never verified" gap this closes. The audit record is
    written BEFORE the check (write-ahead), because a verification that ran and could not be
    recorded is one nobody can later prove happened.
    """
    await request.app.state.auditor.record(
        "audit.verified", actor="system:cron", tenant="", resource="audit_log",
        ip=request.client.host if request.client else "")
    result = await build_audit(request).verify_all()
    if not result["ok"]:
        # 409 CONFLICT: the log's state contradicts its own recorded head. The cron caller raises
        # on any non-2xx, so this fails the Job and fires the alert — the whole point.
        #
        # The break detail is LOGGED, not returned: the platform error handler sanitises 4xx/5xx
        # bodies so an error can never leak internals, and an audit chain break is exactly the kind
        # of internal state that rule protects. The operator reads the partition and reason from
        # the Job's pod log (and the `audit.verified` record sits in the chain either way).
        import logging
        logging.getLogger("audit").error("audit chain verification FAILED: %s", result)
        raise HTTPException(status.HTTP_409_CONFLICT, detail="AUDIT_CHAIN_BROKEN")
    return result


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
    # Audit is the ONE service keyed purely by pseudonym: its events carry subject_ref, never a
    # name. So a caller may legitimately hold only the pseudonym — for instance after identity has
    # already erased the email→subject_ref linkage — and have no raw `subject` to send. It accepts
    # EITHER: `subject` (the raw identity, hashed here) or `subject_ref` (the pseudonym, used
    # directly). Exactly one, because two would let a mismatched pair ack against the wrong person.
    tenant: str
    subject: str = ""
    subject_ref: str = ""

    @model_validator(mode="after")
    def _exactly_one(self) -> "EraseIn":
        if bool(self.subject) == bool(self.subject_ref):
            raise ValueError("provide exactly one of `subject` or `subject_ref`")
        return self


@router.post("/privacy/erase", dependencies=[Depends(require_internal_token)])
async def erase_subject(request: Request, body: EraseIn,
                        settings: Settings = Depends(get_settings)) -> dict:
    """Audit erases NOTHING — and says so, with a basis, and proves the chain still verifies.

    The events carry a pseudonym, not an identity. Once identity-service deletes the linkage they
    name nobody. Redacting them instead would break the hash chain from that seq onward, which is
    the one thing this service exists to make impossible.
    """
    # Resolve to the pseudonym ONCE. A raw subject is hashed to its subject_ref here; a subject_ref
    # is taken as given. Everything downstream — the audit record and the ack — carries the
    # pseudonym, never the raw identity, so this endpoint cannot itself write an email into the
    # very chain that must never hold one.
    ref = body.subject_ref or subject_ref(settings.PII_SALT, body.tenant, body.subject)

    # Write-ahead: a critical action the log cannot take must not happen at all, so this
    # runs BEFORE the mutation. Recording afterwards would 503 a caller whose action had
    # already succeeded, and leave it unrecorded.
    await request.app.state.auditor.record(
        "subject.erased", actor="", tenant=body.tenant, resource=f"subject:{ref}",
        ip=request.client.host if request.client else "")
    return await AuditErasure(audit=build_audit(request)).erase(body.tenant, ref)
