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
from app.core import health as _health
from app.core import alerts as _alerts
from app.core.retention import RetentionSweeper
from app.core.drill import RestoreDrill
from app.core.drills import DrillStore

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
    # Empty = restore in place. A value REMAPS the restore into that tenant (restore-as-clone):
    # only rows that carry a tenant are reassigned; a global dataset is left alone.
    into_tenant: str = ""


def _sealer(settings: Settings):
    """The backup (seal, unseal) pair, or a 503 when no backup key is configured.

    Refused rather than defaulted: sealing with an empty key would write a plaintext-equivalent
    archive while every check reported it encrypted — the one outcome a backup must never have.
    """
    if not settings.BACKUP_KEK:
        raise HTTPException(
            status.HTTP_503_SERVICE_UNAVAILABLE,
            "BACKUP_KEK not configured — set this SHARED key (the SAME value backup-service holds) on this service, or its archives cannot be opened")
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


class BackupVerifyIn(BaseModel):
    archive: dict


@router.post("/backup/verify", dependencies=[Depends(require_internal_token)])
async def backup_verify(request: Request, body: BackupVerifyIn,
                        settings: Settings = Depends(get_settings)) -> dict:
    """Prove an archive restores, by test-restoring it into a throwaway namespace and comparing.

    backup-service calls this after it stores an archive (passing back the bytes it read FROM the
    store, so corruption at rest is caught too). This service is the only party that can unseal its
    own archive and knows its own schema, so verification lives here; the orchestrator only records
    the verdict. The throwaway namespace is torn down before this returns — nothing is left behind,
    and live data is never touched.
    """
    import secrets
    _, unseal = _sealer(settings)
    try:
        return await _backup.verify_restore(
            request.app.state.container.datastore, body.archive,
            unseal=unseal, token=secrets.token_hex(8))
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
            unseal=unseal, overwrite=body.overwrite, into_tenant=body.into_tenant)
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
async def run_backup(request: Request, body: _RunIn | None = None,
                     settings: Settings = Depends(get_settings)) -> dict:
    """Capture every enabled target now. Driven by a cron; also callable for an out-of-band run.

    The run's status is the WORST target's, and a target that could not be reached is MISSED, never
    dropped — so a run cannot report complete while a service was silently skipped.

    BODY IS OPTIONAL, and that is not cosmetic. The platform's scheduled-job template POSTs an EMPTY
    body with a JSON content type; a required model makes FastAPI answer 422 before this function runs,
    so the NIGHTLY BACKUP never executed — the failure living only in a Job's exit code while every
    probe and dashboard stayed green. A cron-driven route must accept the empty POST its own scheduler
    sends (safety:cron-body enforces this platform-wide).
    """
    body = body or _RunIn()
    await request.app.state.auditor.record(
        "backup.run_started", actor="", resource="backup_run",
        ip=request.client.host if request.client else "", metadata={"tenant": body.tenant})
    return await _orchestrator(request, settings).run(tenant=body.tenant)


@router.get("/backup/runs", dependencies=[Depends(require_internal_token)])
async def list_runs(request: Request, settings: Settings = Depends(get_settings)) -> dict:
    return {"runs": await RunStore(request.app.state.container.datastore).recent(settings.BACKUP_RUN_HISTORY)}


# ── health, alerting & retention (46.6) ────────────────────────────────────────
async def _effective_int(request: Request, key: str, default: int) -> int:
    """The operator's runtime override for a policy knob, or the configured default. Read HERE, at
    the point of use, which is the promise RUNTIME_POLICY makes — an advertised knob nothing reads
    is a receipt for a change that never happened."""
    rc = getattr(request.app.state, "runtime_config", None)
    if rc is None:
        return default
    try:
        return int(await rc.get(tenant="", key=key, default=default))
    except (TypeError, ValueError):
        return default


async def _health_report(request: Request, settings: Settings) -> dict:
    c = request.app.state.container
    targets = [t.public() for t in await TargetStore(c.datastore).all(settings.BACKUP_TARGET_LIMIT)]
    runs = await RunStore(c.datastore).recent(settings.BACKUP_RUN_HISTORY)
    stale_after = await _effective_int(request, "BACKUP_STALE_AFTER_SECONDS",
                                       settings.BACKUP_STALE_AFTER_SECONDS)
    return _health.evaluate(targets=targets, runs=runs, now_iso=_now_iso(),
                            stale_after_seconds=stale_after)


@router.get("/backup/health", dependencies=[Depends(require_internal_token)])
async def backup_health(request: Request, settings: Settings = Depends(get_settings)) -> dict:
    """Is there a recent, complete, VERIFIED backup of every service? Read-only — for a monitor to
    scrape. A response of `status: critical` means a service is not protected."""
    return await _health_report(request, settings)


@router.post("/backup/health/check", dependencies=[Depends(require_internal_token)])
async def backup_health_check(request: Request, settings: Settings = Depends(get_settings)) -> dict:
    """Evaluate health and, if it is not green, RECORD and PUSH the alert. Cron-driven.

    The record is written even if the push fails: the alarm surviving a down alert channel is the
    whole point — 'the thing that tells you backups are broken is broken' is when you need it most.
    """
    report = await _health_report(request, settings)
    if report["alerts"]:
        await request.app.state.auditor.record(
            "backup.alerted", actor="", resource="backup_health",
            ip=request.client.host if request.client else "",
            metadata={"status": report["status"],
                      "critical": str(report["summary"]["critical"]),
                      "degraded": str(report["summary"]["degraded"])})
    delivery = _alerts.push(report, webhook_url=settings.BACKUP_ALERT_WEBHOOK_URL,
                            allowed_hosts=settings.egress_allowed_hosts,
                            timeout_seconds=settings.BACKUP_TARGET_TIMEOUT_SECONDS)
    return {**report, "delivery": delivery}


@router.post("/backup/prune", dependencies=[Depends(require_internal_token)])
async def backup_prune(request: Request, settings: Settings = Depends(get_settings)) -> dict:
    """Prune old backups to the retention limits — never the last verified backup of any target.
    Cron-driven. Retention limits are operator-editable at runtime."""
    keep = await _effective_int(request, "BACKUP_RETENTION_RUNS", settings.BACKUP_RETENTION_RUNS)
    max_age = await _effective_int(request, "BACKUP_RETENTION_MAX_AGE_SECONDS",
                                   settings.BACKUP_RETENTION_MAX_AGE_SECONDS)
    sweeper = RetentionSweeper(runs=RunStore(request.app.state.container.datastore),
                               object_store=request.app.state.container.object_store, iso=_now_iso)
    result = await sweeper.prune(keep_runs=keep, max_age_seconds=max_age,
                                 scan_limit=settings.BACKUP_RUN_HISTORY)
    if result["pruned_count"]:
        await request.app.state.auditor.record(
            "backup.pruned", actor="", resource="backup_run",
            ip=request.client.host if request.client else "",
            metadata={"pruned": str(result["pruned_count"]),
                      "archives_deleted": str(result["archives_deleted"])})
    return result


def _drill(request: Request, settings: Settings) -> RestoreDrill:
    from app.core.token_resolver import make_token_resolver
    c = request.app.state.container
    return RestoreDrill(
        targets=TargetStore(c.datastore), object_store=c.object_store, runs=RunStore(c.datastore),
        drills=DrillStore(c.datastore),
        resolve_token=make_token_resolver(getattr(request.app.state, "secret_store", None)),
        iso=_now_iso,
        cfg={"egress_allowed_hosts": settings.egress_allowed_hosts,
             "target_timeout_seconds": settings.BACKUP_TARGET_TIMEOUT_SECONDS,
             "target_limit": settings.BACKUP_TARGET_LIMIT,
             "run_history": settings.BACKUP_RUN_HISTORY})


@router.post("/backup/drill", dependencies=[Depends(require_internal_token)])
async def backup_drill(request: Request, settings: Settings = Depends(get_settings)) -> dict:
    """Prove — right now, against the AT-REST stored bytes — that every service can be restored from
    its best backup. Read-only against live data (the test-restore is in the target's throwaway
    namespace). Cron-driven; records the result and ALERTS if the drill fails.

    This is the difference between 'the records say our backups are fine' and 'we just restored every
    one of them and compared'. A backup that verified at capture but rotted at rest is caught here.
    """
    report = await _drill(request, settings).run()
    failed = report["failed"]
    await request.app.state.auditor.record(
        "backup.drilled", actor="", resource="backup_drill",
        ip=request.client.host if request.client else "",
        metadata={"status": report["status"], "passed": str(len(report["passed"])),
                  "failed": ",".join(failed)})
    if failed:
        # Shape the drill outcome as an alert so the same webhook that watches health hears it.
        alert = {"status": "critical", "checked_at": report["at"],
                 "summary": {"critical": len(failed), "degraded": 0},
                 "alerts": [{"service": s, "severity": "critical",
                             "reason": "restore drill FAILED — best backup no longer restores"}
                            for s in failed]}
        _alerts.push(alert, webhook_url=settings.BACKUP_ALERT_WEBHOOK_URL,
                     allowed_hosts=settings.egress_allowed_hosts,
                     timeout_seconds=settings.BACKUP_TARGET_TIMEOUT_SECONDS)
    return report


@router.get("/backup/drills", dependencies=[Depends(require_internal_token)])
async def list_drills(request: Request, settings: Settings = Depends(get_settings)) -> dict:
    return {"drills": await DrillStore(request.app.state.container.datastore).recent(
        settings.BACKUP_RUN_HISTORY)}
