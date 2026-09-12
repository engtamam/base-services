# =============================================================================
# zatca-service — Internal tier (service-to-service, X-Internal-Token)
# -----------------------------------------------------------------------------
# NOT user-facing. Guarded by a shared token compared in CONSTANT TIME
# (CWE-208: observable timing discrepancy). Never a user JWT.
# =============================================================================
from __future__ import annotations


from typing import Any

from fastapi import APIRouter, Depends, Header, HTTPException, Request, status
from pydantic import BaseModel, Field

from app.api.v1.zatca import build_zatca
from app.backup_manifest import BACKUP
from app.core import backup as _backup
from app.config import Settings, get_settings
from app.core.erasure import ZatcaErasure
from app.manifest import build_manifest
from app.core.pii import subject_ref

router = APIRouter(prefix="/internal", tags=["internal"])


class FinalizeIn(BaseModel):
    tenant: str = Field(min_length=1, max_length=128)
    invoice: dict[str, Any]   # {number, issued_at, seller, buyer?, lines[], currency?, vat_rate?}

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


@router.post("/zatca/finalize", dependencies=[Depends(require_internal_token)])
async def finalize(request: Request, body: FinalizeIn) -> dict:
    """E-invoice one invoice (any service calls this after settling). Never fails the
    caller — a transient error leaves the invoice retryable ('pending')."""
    if not body.invoice.get("number"):
        raise HTTPException(422, "MISSING_NUMBER")
    from app.core.zatca_service import ZatcaInputError
    try:
        # Pass the invoice's tenant so signing resolves THIS tenant's signer credential (audit-02).
        result = await (await build_zatca(request, body.tenant)).finalize(body.invoice, body.tenant)
    except ZatcaInputError as exc:
        # A malformed payload is a permanent caller error, not the transient kind the 'never fails'
        # guarantee covers — a clean 422, and no ICV was burned (validated before slot allocation).
        raise HTTPException(422, f"INVALID_INVOICE: {exc}") from exc
    # Called service-to-service after a settle, so no user principal — actor is empty, the source IP
    # names the peer. The invoice number is the resource an auditor reconciles against ZATCA.
    await request.app.state.auditor.record(
        "zatca.invoice_finalized", actor="", tenant=body.tenant,
        resource=f"invoice:{body.invoice.get('number', '')}",
        ip=request.client.host if request.client else "")
    return result


@router.post("/zatca/sweep", dependencies=[Depends(require_internal_token)])
async def internal_sweep(request: Request) -> dict:
    result = await (await build_zatca(request)).sweep()
    await request.app.state.auditor.record(
        "zatca.swept", actor="system", resource="zatca",
        ip=request.client.host if request.client else "")
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
    """zatca deletes NO legal record — it CANNOT, and the ack says why.

    A signed invoice carries a ZATCA signature over its own bytes and its hash is the next
    invoice's PIH, so altering one invoice would invalidate every invoice after it. Unlike billing,
    the buyer's identity here cannot even be pseudonymised. What IS erased is the unsigned draft
    working copy of already-accepted invoices: real personal data, no legal significance.
    """
    # Write-ahead: a critical action the log cannot take must not happen at all, so this
    # runs BEFORE the mutation. Recording afterwards would 503 a caller whose action had
    # already succeeded, and leave it unrecorded.
    await request.app.state.auditor.record(
        "subject.erased", actor="", tenant=body.tenant, resource=f"subject:{subject_ref(get_settings().PII_SALT, body.tenant, body.subject)}",
        ip=request.client.host if request.client else "")
    c = request.app.state.container
    s = get_settings()
    eraser = ZatcaErasure(store=c.datastore, pii_salt=s.PII_SALT,
                          retention_years=s.TAX_RETENTION_YEARS, max_scan=s.ERASURE_MAX_SCAN)
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


async def _rebuild_index(datastore, scan_cap: int) -> dict[str, int]:
    """Rebuild zatca_index from the restored invoices — it is DERIVED, not archived.

    Per tenant: the number lookup (num::<tenant>::<number> -> {id}) for every invoice, and the chain
    head (head::<tenant> -> {reserved_icv, last_hash}). reserved_icv is the max ICV — allocate_slot
    persists an invoice for every ICV it reserves, so no reserved slot is missed and the next
    allocation cannot re-issue a number. last_hash follows the highest SIGNED invoice (the one whose
    hash the chain extends from), exactly as record_hash maintains it. Idempotent and self-healing.
    """
    heads: dict[str, dict[str, Any]] = {}   # tenant -> {reserved_icv, last_hash, _top_signed_icv}
    nums = 0
    for row in await datastore.scan_all("zatca_invoices", scan_cap):
        tenant, number, iid = row.get("tenant"), row.get("number"), row.get("id")
        icv, ihash = int(row.get("icv", 0) or 0), row.get("hash") or ""
        if not tenant or not iid:
            continue
        if number is not None:
            await datastore.put("zatca_index", f"num::{tenant}::{number}", {"id": iid})
            nums += 1
        h = heads.setdefault(tenant, {"reserved_icv": 0, "last_hash": "", "_top_signed_icv": -1})
        h["reserved_icv"] = max(int(h["reserved_icv"]), icv)
        if ihash and icv > h["_top_signed_icv"]:     # the highest SIGNED invoice sets the head hash
            h["_top_signed_icv"] = icv
            h["last_hash"] = ihash
    for tenant, h in heads.items():
        await datastore.put("zatca_index", f"head::{tenant}",
                            {"reserved_icv": h["reserved_icv"], "last_hash": h["last_hash"]})
    return {"num_entries": nums, "heads": len(heads)}


@router.post("/backup/import", dependencies=[Depends(require_internal_token)])
async def backup_import(request: Request, body: BackupImportIn,
                        settings: Settings = Depends(get_settings)) -> dict:
    """Restore an archive this service produced. Idempotent; leaves newer rows alone by default.

    After the invoices are restored, the ICV chain head and number index are rebuilt from them — a
    restore that skipped them would let the next invoice re-issue an ICV that already exists, which
    is a ZATCA compliance defect, not a glitch.
    """
    datastore = request.app.state.container.datastore
    _, unseal = _sealer(settings)
    try:
        result = await _backup.import_archive(
            datastore, body.archive,
            unseal=unseal, overwrite=body.overwrite, into_tenant=body.into_tenant,
            declaration=BACKUP)
    except _backup.BackupError as exc:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, str(exc)) from exc
    result["index_rebuilt"] = await _rebuild_index(
        datastore, settings.BACKUP_MAX_ROWS_PER_COLLECTION)
    return result
