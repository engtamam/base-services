# =============================================================================
# ticket-service — Internal tier (service-to-service, X-Internal-Token)
# -----------------------------------------------------------------------------
# NOT user-facing. Guarded by a shared token compared in CONSTANT TIME
# (CWE-208: observable timing discrepancy). Never a user JWT.
# =============================================================================
from __future__ import annotations

import hmac

from pydantic import BaseModel

from fastapi import APIRouter, Depends, Header, HTTPException, Request, status

from app.config import Settings, get_settings
from app.core import backup as _backup
from app.backup_manifest import BACKUP
from app.manifest import build_manifest

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
    # Write-ahead: erasure is irreversible and critical — if it cannot be recorded it must not
    # happen. Actor is empty: this is a service-to-service DSAR call, not a person.
    await request.app.state.auditor.record(
        "subject.erased", actor="", tenant=body.tenant, resource=f"subject:{body.subject}",
        ip=request.client.host if request.client else "")
    from app.core.erasure import TicketErasure
    c = request.app.state.container
    s = get_settings()
    eraser = TicketErasure(store=c.datastore, pii_salt=s.PII_SALT, max_scan=s.ERASURE_MAX_SCAN)
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


# ── scoped cross-service ticket visibility (a shop's consented share to one named party) ────────────
# See core/share.py. The GENERAL primitive between tickets.self (too narrow) and platform.tickets (too
# broad): a tenant consents to ONE outside party (grantee) reading a scoped subset of its tickets. This
# service holds and ENFORCES the grant — a peer presents (tenant, grantee) and gets only what consent
# allows. The reads carry STRUCTURE + PERFORMANCE (KPIs), never free text (subject/body may hold the
# shop's customer PII, which is not the grantee's to read); actors are pseudonyms, as everywhere here.
def _stores(request: Request):
    from app.core.share import ShareStore
    from app.core.stores import EventStore, TicketStore
    ds = request.app.state.container.datastore
    return TicketStore(ds), EventStore(ds), ShareStore(ds)


def _project(row: dict) -> dict:
    """A grantee-safe ticket view: structure, status and timing only — NO subject, body or meta."""
    return {"id": row.get("id", ""), "type": row.get("type", ""), "status": row.get("status", ""),
            "priority": row.get("priority", "normal"), "created": row.get("created", ""),
            "updated": row.get("updated", ""), "assignee": row.get("assignee", ""),
            "tags": list(row.get("tags") or []), "raiser_ref": row.get("raiser_ref", "")}


async def _active_grant(request: Request, tenant: str, grantee: str):
    """The active consent grant for (tenant, grantee), or None. Fails CLOSED: a revoked or expired
    grant is None, so a read over it returns nothing."""
    import time
    _, _, shares = _stores(request)
    g = await shares.get(tenant, grantee)
    return g if (g is not None and g.is_active(time.time())) else None


class ShareIn(BaseModel):
    tenant: str
    grantee: str                                  # a stable, opaque handle for the ONE outside party
    granted_by: str = ""                          # who consented (a shop id); stored ONLY as a pseudonym
    scope: dict = {}                              # {"type":[...], "status":[...]}; {} = all of the tenant
    ttl_seconds: int = 0                          # 0 = use the configured default (which may also be 0)


@router.post("/tickets/shares", dependencies=[Depends(require_internal_token)])
async def grant_share(request: Request, body: ShareIn,
                      settings: Settings = Depends(get_settings)) -> dict:
    """Record a shop's CONSENT to let one named party read a scoped subset of its tickets (spec §13).

    Idempotent: one grant per (tenant, grantee), so recording consent twice is one upsert. granted_by
    is stored ONLY as a per-tenant pseudonym — who consented is a fact, their raw identity is not this
    service's to keep. Audited write-ahead, because a consent record that cannot be logged must not be
    created: a grant nobody can prove was given is indistinguishable from one an attacker planted."""
    import time

    from app.core.pii import subject_ref
    from app.core.share import ShareGrant
    if not body.tenant or not body.grantee:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "tenant and grantee are required")
    granted_by = subject_ref(settings.PII_SALT, body.tenant, body.granted_by) if body.granted_by else ""
    await request.app.state.auditor.record(
        "ticket.share_granted", actor=granted_by, tenant=body.tenant,
        resource=f"grantee:{body.grantee}", ip=request.client.host if request.client else "")
    ttl = body.ttl_seconds or settings.TICKET_SHARE_DEFAULT_TTL_SECONDS
    grant = ShareGrant(tenant=body.tenant, grantee=body.grantee, scope=dict(body.scope or {}),
                       granted_by=granted_by, granted_at=_now_iso(),
                       expires_at=(time.time() + ttl) if ttl > 0 else 0.0)
    _, _, shares = _stores(request)
    await shares.upsert(grant)
    return {"granted": True, **grant.to_dict()}


@router.delete("/tickets/shares/{grantee}", dependencies=[Depends(require_internal_token)])
async def revoke_share(request: Request, grantee: str, tenant: str) -> dict:
    """Withdraw a share. Idempotent (revoking an absent/already-revoked grant is success), kept as a
    tombstone so 'consent was withdrawn' stays answerable. Audited write-ahead."""
    if not tenant:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "tenant is required")
    await request.app.state.auditor.record(
        "ticket.share_revoked", actor="", tenant=tenant,
        resource=f"grantee:{grantee}", ip=request.client.host if request.client else "")
    _, _, shares = _stores(request)
    return {"revoked": await shares.revoke(tenant, grantee)}


@router.get("/tickets/shares", dependencies=[Depends(require_internal_token)])
async def list_shares(request: Request, tenant: str,
                      settings: Settings = Depends(get_settings)) -> dict:
    """Every party a tenant has shared with — so a shop can audit exactly who it consented to."""
    if not tenant:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "tenant is required")
    _, _, shares = _stores(request)
    rows = await shares.list_for_tenant(tenant, limit=settings.TICKET_SHARE_LIST_MAX)
    return {"shares": rows}


@router.get("/tickets", dependencies=[Depends(require_internal_token)])
async def list_shared_tickets(request: Request, tenant: str, grantee: str,
                              settings: Settings = Depends(get_settings)) -> dict:
    """A grantee's consented, scoped view of a tenant's tickets — structure and status only, no content.

    Requires an ACTIVE grant for (tenant, grantee); without one it refuses (409 NO_ACTIVE_GRANT) rather
    than leak that the tenant has tickets at all. One bounded, indexed find (never a scan); `truncated`
    is loud."""
    if not tenant or not grantee:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "tenant and grantee are required")
    from starlette.responses import JSONResponse
    grant = await _active_grant(request, tenant, grantee)
    if grant is None:
        return JSONResponse({"error": "NO_ACTIVE_GRANT",
                             "reason": "no active consent grant for this grantee"}, status_code=409)
    tickets, _, _ = _stores(request)
    cap = settings.TICKET_SHARE_LIST_MAX
    rows, cursor = await tickets.find(tenant=tenant, limit=cap + 1)
    covered = [r for r in rows if grant.covers(r)]
    truncated = len(covered) > cap
    return {"tenant": tenant, "grantee": grantee,
            "tickets": [_project(r) for r in covered[:cap]], "truncated": truncated}


@router.get("/tickets/{ticket_id}", dependencies=[Depends(require_internal_token)])
async def read_shared_ticket(request: Request, ticket_id: str, tenant: str, grantee: str,
                             settings: Settings = Depends(get_settings)) -> dict:
    """One ticket a grantee may see, WITH its derived KPIs (first-response speed, escalation, resolution
    — kpis.ticket_kpis over the event log). Grant-checked and scope-checked; a ticket the grant does not
    cover answers 404 exactly like a missing one, so the grant can never be used to probe existence."""
    if not tenant or not grantee:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "tenant and grantee are required")
    from starlette.responses import JSONResponse
    from app.core.kpis import ticket_kpis
    grant = await _active_grant(request, tenant, grantee)
    if grant is None:
        return JSONResponse({"error": "NO_ACTIVE_GRANT",
                             "reason": "no active consent grant for this grantee"}, status_code=409)
    tickets, events, _ = _stores(request)
    t = await tickets.fetch(ticket_id)
    row = t.to_dict() if t is not None else None
    if row is None or row.get("tenant") != tenant or not grant.covers(row):
        raise HTTPException(status.HTTP_404_NOT_FOUND, "NOT_FOUND")
    hist, _ = await events.history(tenant=tenant, ticket_id=ticket_id, limit=settings.ERASURE_MAX_SCAN)
    return {"ticket": _project(row), "kpis": ticket_kpis(row, hist)}


@router.post("/tickets/notifications/drain", dependencies=[Depends(require_internal_token)])
async def drain_notifications(request: Request,
                              settings: Settings = Depends(get_settings)) -> dict:
    """Deliver every queued ticket notification to notification-service.

    Driven by the platform's scheduler rather than a thread in here, so a restart cannot lose the
    only thing that was retrying, and so an operator can see the schedule. A null transport records
    but delivers nothing; pointing NOTIFY_DRIVER at http later delivers everything accumulated."""
    import time
    from app.core.notifier import NotifierOutbox
    c = request.app.state.container
    outbox = NotifierOutbox(
        c.datastore, c.notification_sender,
        cfg={"retry_base_seconds": settings.NOTIFY_RETRY_BASE_SECONDS,
             "retry_max_seconds": settings.NOTIFY_RETRY_MAX_SECONDS},
        clock=time.monotonic)
    return await outbox.drain(limit=settings.NOTIFY_DRAIN_LIMIT)
