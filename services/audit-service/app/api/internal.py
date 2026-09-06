# =============================================================================
# audit-service — Internal tier (service-to-service, X-Internal-Token)
# -----------------------------------------------------------------------------
# NOT user-facing. Guarded by a shared token compared in CONSTANT TIME
# (CWE-208: observable timing discrepancy). Never a user JWT.
# =============================================================================
from __future__ import annotations


from fastapi import APIRouter, Depends, Header, HTTPException, Query, Request, status
from pydantic import BaseModel, Field, model_validator

from app.api.v1.audit import build_audit
from app.backup_manifest import BACKUP
from app.config import Settings, get_settings
from app.core import backup as _backup
from app.core.audit_service import IngestInProgress
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
    # A client-generated key, stable across a caller's own retries of ONE action. With it the sink
    # can retry a transient timeout safely: a repeat carries the same key, the log returns the
    # original receipt instead of appending a duplicate to the tamper-evident chain. Empty = the
    # legacy one-shot behaviour (a caller that sends no key gets exactly what it did before).
    idempotency_key: str = Field(default="", max_length=128)

_HEADER = "X-Internal-Token"
_ASSERTION_HEADER = "X-Internal-Assertion"


async def require_internal_token(
    request: Request,
    x_internal_token: str | None = Header(default=None, alias=_HEADER),
    x_internal_assertion: str | None = Header(default=None, alias=_ASSERTION_HEADER),
    settings: Settings = Depends(get_settings),
) -> None:
    """Authenticate the internal caller, then authorise it for THIS route. The name is unchanged so
    every existing `Depends(require_internal_token)` keeps working, but it now does two things:

      1. IDENTITY — a config-resolved (cached) driver verifies the caller. `shared` (default) is
         the timing-safe flat-token compare (today's behaviour, unnamed peer); `static`/`jwks` verify a
         per-caller RS256 assertion. Any failure is one generic 401 — never which check failed.
      2. POLICY — the per-route allow-list decides whether that caller may reach `request.url.path`. A
         route with no policy admits any authenticated caller (default-open), so identity alone never
         breaks a path; a named allow-list yields 403 for a caller not on it.

    The verifier + policy are resolved from config (cached), not the container, so this seam is
    identical in every service."""
    from app.core.internal_caller_factory import resolve_internal_caller, resolve_internal_policy
    from app.ports.internal_caller import InternalCallerError

    audience = settings.INTERNAL_CALLER_AUDIENCE or settings.SERVICE_NAME
    try:
        identity = resolve_internal_caller(settings).verify(
            token=x_internal_token or "", assertion=x_internal_assertion or "", audience=audience)
    except InternalCallerError:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "UNAUTHORIZED")
    if not resolve_internal_policy(settings).permits(request.url.path, identity.caller):
        raise HTTPException(status.HTTP_403_FORBIDDEN, "FORBIDDEN")


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
    audit = build_audit(request)
    result = await audit.enforce_retention(settings.RETENTION_DAYS, holds=holds)
    # Housekeeping: drop idempotency keys past the retry window (their receipts live in the chain).
    result["idempotency_keys_pruned"] = await audit.prune_idempotency(
        settings.AUDIT_IDEMPOTENCY_TTL_SECONDS)
    return result


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


@router.get("/audit", dependencies=[Depends(require_internal_token)])
async def internal_query(request: Request,
                         tenant: str = Query(..., min_length=1, max_length=128),
                         limit: int | None = Query(default=None, le=1000),
                         service: str | None = Query(default=None, max_length=128),
                         actor: str | None = Query(default=None, max_length=256),
                         action: str | None = Query(default=None, max_length=128),
                         resource: str | None = Query(default=None, max_length=256),
                         since: str | None = Query(default=None, max_length=64),
                         until: str | None = Query(default=None, max_length=64),
                         after: str | None = Query(default=None, max_length=512)) -> dict:
    """Internal-token twin of GET /api/v1/audit — a service (the case-timeline investigation tool)
    reading a customer's audit trail across services. Same query() logic and filter set as the
    user-tier route; tenant is REQUIRED here (an internal caller names the tenant it is investigating,
    like every other /internal route) and scopes every read, so this is never a cross-tenant dump.

    PAGEABLE: tenant is always set, so this is a single-partition read — pass `next_cursor` back as
    `after` to page through a tenant's full history (a dispute report reaching an event older than
    one page) instead of being hard-capped at `limit`."""
    filters: dict = {"tenant": tenant}
    for key, val in (("service", service), ("actor", actor), ("action", action),
                     ("resource", resource), ("since", since), ("until", until)):
        if val:
            filters[key] = val
    events, next_cursor = await build_audit(request).query(filters, limit, after=after)
    # The access trail is only worth having if reading it is also on it (same rationale as the
    # user-tier route). Internal caller ⇒ actor is empty; the tenant is the one being investigated.
    await request.app.state.auditor.record(
        "audit.queried", actor="", tenant=tenant, resource="audit_log",
        ip=request.client.host if request.client else "",
        metadata={"returned": str(len(events))})
    return {"events": events, "count": len(events), "next_cursor": next_cursor}


@router.post("/audit", dependencies=[Depends(require_internal_token)])
async def internal_audit(request: Request, body: AuditIn,
                         settings: Settings = Depends(get_settings)) -> dict:
    """Primary ingest — a service records an audit event here (service-to-service).

    Idempotent when the caller supplies `idempotency_key`: a retried append (after a transient
    timeout) returns the ORIGINAL receipt with `duplicate: true` and adds nothing to the chain.
    """
    try:
        receipt, duplicate = await build_audit(request).record_idempotent(
            idempotency_key=body.idempotency_key,
            stale_seconds=settings.AUDIT_INGEST_STALE_SECONDS,
            service=body.service, action=body.action, actor=body.actor,
            resource=body.resource, tenant=body.tenant, ip=body.ip, metadata=body.metadata,
        )
    except IngestInProgress:
        # Another attempt with this key is mid-append. A 503 tells the caller to retry (the sink
        # does, with the same key, backing off); a later attempt returns the committed receipt, so
        # exactly one event is ever appended even when a slow first append overlaps its own retry.
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, "AUDIT_INGEST_IN_PROGRESS")
    return {**receipt, "duplicate": duplicate}


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


# ── Backup (backup-service orchestrates; this service produces sealed archives) ──────────────
# What audit-service holds is declared in app/backup_manifest.py; how it is read lives in
# core/backup.py. This is the tamper-evident witness log — the events and their chain head travel
# together so a restored chain still verifies. backup-service never opens this database.
def _now_iso() -> str:
    import datetime as _dt
    return _dt.datetime.now(_dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _sealer(settings: Settings):
    """The backup (seal, unseal) pair, or a 503 when no backup key is configured.

    Refused rather than defaulted: sealing with an empty key would write a plaintext-equivalent
    archive while every check reported it encrypted — the one outcome a backup must never have.
    """
    if not settings.BACKUP_KEK:
        raise HTTPException(
            status.HTTP_503_SERVICE_UNAVAILABLE,
            "BACKUP_KEK not configured — set this SHARED key (the SAME value backup-service holds) "
            "on this service, or its archives cannot be opened")
    return _backup.make_sealer(settings.BACKUP_KEK)


class BackupExportIn(BaseModel):
    tenant: str = ""


class BackupVerifyIn(BaseModel):
    archive: dict


class BackupImportIn(BaseModel):
    archive: dict
    overwrite: bool = False
    into_tenant: str = ""


@router.post("/backup/export", dependencies=[Depends(require_internal_token)])
async def backup_export(request: Request, body: BackupExportIn,
                        settings: Settings = Depends(get_settings)) -> dict:
    """Produce a sealed, checksummed archive of the audit chain and its per-partition head."""
    seal, _ = _sealer(settings)
    return await _backup.export(
        request.app.state.container.datastore, BACKUP,
        service=settings.SERVICE_NAME, version=settings.SERVICE_VERSION,
        taken_at=_now_iso(), seal=seal, tenant=body.tenant,
        scan_cap=settings.BACKUP_MAX_ROWS_PER_COLLECTION)


@router.post("/backup/verify", dependencies=[Depends(require_internal_token)])
async def backup_verify(request: Request, body: BackupVerifyIn,
                        settings: Settings = Depends(get_settings)) -> dict:
    """Prove an archive restores, by test-restoring it into a throwaway namespace and comparing.

    The declaration is passed so audit_meta's "{partition}" key is reconstructed for the verify.
    """
    import secrets
    _, unseal = _sealer(settings)
    try:
        return await _backup.verify_restore(
            request.app.state.container.datastore, body.archive,
            unseal=unseal, token=secrets.token_hex(8), declaration=BACKUP)
    except _backup.BackupError as exc:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, str(exc)) from exc


@router.post("/backup/import", dependencies=[Depends(require_internal_token)])
async def backup_import(request: Request, body: BackupImportIn,
                        settings: Settings = Depends(get_settings)) -> dict:
    """Restore an archive this service produced. Idempotent; leaves newer rows alone by default.

    The declaration is passed so audit_meta restores under its partition key — the chain head must
    land under the same key `append()` reads, or the next event links to the wrong hash.
    """
    _, unseal = _sealer(settings)
    try:
        return await _backup.import_archive(
            request.app.state.container.datastore, body.archive,
            unseal=unseal, overwrite=body.overwrite, into_tenant=body.into_tenant,
            declaration=BACKUP)
    except _backup.BackupError as exc:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, str(exc)) from exc
