# =============================================================================
# connector-service — Internal tier (service-to-service, X-Internal-Token)
# -----------------------------------------------------------------------------
# NOT user-facing. Guarded by a shared token compared in CONSTANT TIME
# (CWE-208: observable timing discrepancy). Never a user JWT.
# =============================================================================
from __future__ import annotations


from typing import Any

from fastapi import APIRouter, Depends, Header, HTTPException, Request, status
from pydantic import BaseModel, Field

from app.api.v1.connectors import _spec_for, build_sync
from app.backup_manifest import BACKUP
from app.core import backup as _backup
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
        result = await engine.pull(connection_id=conn.id, resource=body.resource, spec=spec,
                                   field_map=field_map)
    else:
        result = await engine.push(connection_id=conn.id, resource=body.resource,
                                   records=body.records, spec=spec, field_map=field_map)
    # The durable, scheduler-driven run (job-service retries this) — actor "system".
    await request.app.state.auditor.record(
        "connector.sync_run", actor="system", tenant=getattr(conn, "tenant", "") or "",
        resource=f"connection:{conn.id}", ip=request.client.host if request.client else "",
        metadata={"direction": body.direction, "resource": body.resource})
    return result


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


# ── Backup (backup-service orchestrates; this service produces sealed archives) ──────────────
# What this service holds is declared in app/backup_manifest.py; how it is read lives in
# core/backup.py. backup-service never opens this database — it calls export/verify/import over the
# internal token and stores the sealed archive, never seeing the plaintext.
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
    """Produce a sealed, checksummed archive of this service's declared data."""
    seal, _ = _sealer(settings)
    return await _backup.export(
        request.app.state.container.datastore, BACKUP,
        service=settings.SERVICE_NAME, version=settings.SERVICE_VERSION,
        taken_at=_now_iso(), seal=seal, tenant=body.tenant,
        scan_cap=settings.BACKUP_MAX_ROWS_PER_COLLECTION)


@router.post("/backup/verify", dependencies=[Depends(require_internal_token)])
async def backup_verify(request: Request, body: BackupVerifyIn,
                        settings: Settings = Depends(get_settings)) -> dict:
    """Prove an archive restores, by test-restoring it into a throwaway namespace and comparing."""
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
    """Restore an archive this service produced. Idempotent; leaves newer rows alone by default."""
    _, unseal = _sealer(settings)
    try:
        return await _backup.import_archive(
            request.app.state.container.datastore, body.archive,
            unseal=unseal, overwrite=body.overwrite, into_tenant=body.into_tenant,
            declaration=BACKUP)
    except _backup.BackupError as exc:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, str(exc)) from exc
