# =============================================================================
# backup-service — Internal tier (service-to-service, X-Internal-Token)
# -----------------------------------------------------------------------------
# NOT user-facing. Guarded by a shared token compared in CONSTANT TIME
# (CWE-208: observable timing discrepancy). Never a user JWT.
# =============================================================================
from __future__ import annotations

import hmac

from pydantic import BaseModel, Field

from fastapi import APIRouter, Depends, Header, HTTPException, Request, status

from app.config import Settings, get_settings
from app.core.erasure import TemplateErasure
from app.core import backup as _backup
from app.backup_manifest import BACKUP
from app.manifest import build_manifest

from app.core.targets import Target, TargetStore
from app.core.runs import RunStore
from app.core.orchestrator import Orchestrator

router = APIRouter(prefix="/internal", tags=["internal"])


def _now_iso() -> str:
    import datetime as _dt
    return _dt.datetime.now(_dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

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
    """Erase a data subject. Called by compliance-service during a DSAR fan-out.

    Returns the ack: what was erased, and what was RETAINED with its legal basis. Idempotent — a
    DSAR retries, so erasing an already-erased subject must not fail. Wire this service into the
    fan-out by adding it to PRIVACY_TARGETS in compliance-service's values file; the
    safety:privacy-wiring gate fails if you implement this and forget.
    """
    c = request.app.state.container
    s = get_settings()
    # Recorded write-ahead: a DSAR the log cannot witness must not silently proceed. backup-service
    # holds no subject data in its own collections (targets and runs), so this erase is a genuine
    # no-op over its own store — but the fan-out derives completion from acks, so the ack and its
    # audit trail are the point, not the row count.
    await request.app.state.auditor.record(
        "subject.erased", actor="", tenant=body.tenant, resource="subject",
        ip=request.client.host if request.client else "")
    eraser = TemplateErasure(store=c.datastore, pii_salt=s.PII_SALT,
                             max_scan=s.ERASURE_MAX_SCAN)
    return await eraser.erase(body.tenant, body.subject)


class BackupExportIn(BaseModel):
    # Empty tenant = the whole service; a value = just that customer's tenant-scoped rows.
    tenant: str = ""


class BackupImportIn(BaseModel):
    archive: dict
    # Off by default: a restore must not bulldoze rows written after the backup unless the operator
    # driving the restore says so.
    overwrite: bool = False


def _sealer(settings: Settings):
    """The backup (seal, unseal) pair, or a 503 when no backup key is configured.

    Refused rather than defaulted: sealing with an empty key would write a plaintext-equivalent
    archive while every check reported it encrypted — the one outcome a backup must never have.
    """
    if not settings.BACKUP_KEK:
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, "BACKUP_UNAVAILABLE")
    return _backup.make_sealer(settings.BACKUP_KEK)


@router.post("/backup/export", dependencies=[Depends(require_internal_token)])
async def backup_export(request: Request, body: BackupExportIn,
                        settings: Settings = Depends(get_settings)) -> dict:
    """Produce a sealed, checksummed archive of this service's declared data.

    Called by backup-service, on a schedule, over the internal token. What this service holds is
    declared in app/backup_manifest.py; how it is read lives in core/backup.py. The orchestrator
    stores the archive and never sees the plaintext.
    """
    seal, _ = _sealer(settings)
    return await _backup.export(
        request.app.state.container.datastore, BACKUP,
        service=settings.SERVICE_NAME, version=settings.SERVICE_VERSION,
        taken_at=_now_iso(), seal=seal, tenant=body.tenant,
        scan_cap=settings.BACKUP_MAX_ROWS_PER_COLLECTION)


@router.post("/backup/import", dependencies=[Depends(require_internal_token)])
async def backup_import(request: Request, body: BackupImportIn,
                        settings: Settings = Depends(get_settings)) -> dict:
    """Restore an archive this service produced. Idempotent; leaves newer rows alone by default."""
    _, unseal = _sealer(settings)
    try:
        return await _backup.import_archive(
            request.app.state.container.datastore, body.archive,
            unseal=unseal, overwrite=body.overwrite)
    except _backup.BackupError as exc:
        # A bad archive is the caller's problem, not a 500: the checksum failed, the key did not
        # match, or a row had no identity. Say which, so an operator can act.
        raise HTTPException(status.HTTP_400_BAD_REQUEST, str(exc)) from exc


class _TargetIn(BaseModel):
    service: str = Field(min_length=1, max_length=128)
    base_url: str = Field(min_length=1, max_length=512)
    token_ref: str = Field(default="", max_length=128)   # the operator-secret NAME, never the token
    enabled: bool = True
    note: str = Field(default="", max_length=512)


class _RunIn(BaseModel):
    tenant: str = ""


def _now_iso() -> str:
    import datetime as _dt
    return _dt.datetime.now(_dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _orchestrator(request: Request, settings: Settings) -> Orchestrator:
    from app.core.token_resolver import make_token_resolver
    c = request.app.state.container
    resolve_token = make_token_resolver(getattr(request.app.state, "secret_store", None))

    return Orchestrator(
        targets=TargetStore(c.datastore), object_store=c.object_store, runs=RunStore(c.datastore),
        resolve_token=resolve_token, clock=_now_iso, iso=_now_iso,
        cfg={"egress_allowed_hosts": settings.egress_allowed_hosts,
             "target_timeout_seconds": settings.BACKUP_TARGET_TIMEOUT_SECONDS,
             "target_limit": settings.BACKUP_TARGET_LIMIT})


@router.post("/backup/targets", dependencies=[Depends(require_internal_token)])
async def register_target(request: Request, body: _TargetIn,
                          settings: Settings = Depends(get_settings)) -> dict:
    """Register (or update) a service backup-service protects. First- or third-party, same shape."""
    await request.app.state.auditor.record(
        "backup.target_registered", actor="", resource=f"backup_target:{body.service}",
        ip=request.client.host if request.client else "", metadata={"base_url": body.base_url})
    t = await TargetStore(request.app.state.container.datastore).upsert(
        Target(service=body.service, base_url=body.base_url, token_ref=body.token_ref,
               enabled=body.enabled, created=_now_iso(), note=body.note))
    return t.public()


@router.get("/backup/targets", dependencies=[Depends(require_internal_token)])
async def list_targets(request: Request, settings: Settings = Depends(get_settings)) -> dict:
    targets = await TargetStore(request.app.state.container.datastore).all(settings.BACKUP_TARGET_LIMIT)
    return {"targets": [t.public() for t in targets]}


@router.delete("/backup/targets/{service}", dependencies=[Depends(require_internal_token)])
async def remove_target(request: Request, service: str) -> dict:
    return {"removed": await TargetStore(request.app.state.container.datastore).remove(service)}


@router.post("/backup/run", dependencies=[Depends(require_internal_token)])
async def run_backup(request: Request, body: _RunIn,
                     settings: Settings = Depends(get_settings)) -> dict:
    """Capture every enabled target now. Driven by a cron; also callable for an out-of-band run.

    The run's status is the WORST target's, and a target that could not be reached is MISSED, never
    dropped — so a run cannot report complete while a service was silently skipped.
    """
    await request.app.state.auditor.record(
        "backup.run_started", actor="", resource="backup_run",
        ip=request.client.host if request.client else "", metadata={"tenant": body.tenant})
    return await _orchestrator(request, settings).run(tenant=body.tenant)


@router.get("/backup/runs", dependencies=[Depends(require_internal_token)])
async def list_runs(request: Request, settings: Settings = Depends(get_settings)) -> dict:
    return {"runs": await RunStore(request.app.state.container.datastore).recent(settings.BACKUP_RUN_HISTORY)}
