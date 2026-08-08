# =============================================================================
# notification-service — Internal tier (service-to-service, X-Internal-Token)
# -----------------------------------------------------------------------------
# NOT user-facing. Guarded by a shared token compared in CONSTANT TIME
# (CWE-208: observable timing discrepancy). Never a user JWT.
# =============================================================================
from __future__ import annotations

import hmac

from fastapi import APIRouter, Depends, Header, HTTPException, Request, status
from pydantic import BaseModel, Field

from app.api.v1.notifications import build_notifier
from app.backup_manifest import BACKUP
from app.core import backup as _backup
from app.config import Settings, get_settings
from app.core.erasure import NotificationErasure
from app.core.notifier import CHANNELS
from app.manifest import build_manifest

# The channels /internal/notify accepts, DERIVED from the notifier's authoritative tuple rather than
# restated. This endpoint is the only send path in the service; when its pattern and core/notifier's
# CHANNELS drifted, `whatsapp` was rejected at the door (422) while the engine behind it fully
# supported the channel — silently disabling the one channel this platform is built around. Deriving
# the pattern is what stops the two lists from ever falling out of step again.
_CHANNEL_PATTERN = "^(" + "|".join(CHANNELS) + ")$"

router = APIRouter(prefix="/internal", tags=["internal"])


class NotifyIn(BaseModel):
    recipient: str = Field(min_length=1, max_length=256)
    channel: str = Field(pattern=_CHANNEL_PATTERN)   # derived from core/notifier.CHANNELS
    template_key: str = Field(min_length=1, max_length=128)
    # The tenant a consent-gated send is checked against. Consent is keyed by (tenant, subject,
    # purpose): without this field pydantic silently DROPS a caller's tenant, every gated check asks
    # about an empty tenant, finds no record, and returns consent_denied — a permanent, silent deny
    # for every marketing/consent-gated notification. Ungated (contract-basis) templates are
    # unaffected. Optional because contract-basis sends need no tenant.
    tenant: str | None = Field(default=None, max_length=128)
    locale: str | None = None
    variables: dict = Field(default_factory=dict)
    user_id: str | None = None
    event: str | None = None
    idempotency_key: str | None = Field(default=None, max_length=256)

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


@router.post("/notify", dependencies=[Depends(require_internal_token)])
async def internal_notify(request: Request, body: NotifyIn) -> dict:
    """Service-to-service send. Returns the delivery id + status."""
    # body.tenant, not getattr(body, 'tenant', ''): the defensive access is what turned a missing
    # field from a loud AttributeError on the first request into a silent wrong answer. And the
    # tenant must reach .notify() too — build_notifier's tenant only scopes the provider pool, while
    # the consent gate reads notify()'s own tenant argument.
    return await (await build_notifier(request, body.tenant or "")).notify(
        recipient=body.recipient, channel=body.channel, template_key=body.template_key,
        tenant=body.tenant, locale=body.locale, variables=body.variables,
        user_id=body.user_id, event=body.event, idempotency_key=body.idempotency_key,
    )


@router.post("/notifications/drain", dependencies=[Depends(require_internal_token)])
async def drain_queue(request: Request) -> dict:
    """Re-attempt every queued delivery whose backoff has expired.

    A retry nobody runs is not a retry — `queued` would just be a prettier word for `lost`. Drive
    this from the chart's cronJobs (it exists for exactly this), or any scheduler:

        cronJobs:
          - name: notify-drain
            schedule: "*/1 * * * *"
            path: /internal/notifications/drain

    Idempotent and safe to run concurrently-ish: each delivery carries its own next_attempt_ts, so
    a tick that arrives early simply skips it.
    """
    s = get_settings()
    result = await (await build_notifier(request)).drain(limit=s.NOTIFY_DRAIN_LIMIT)
    await request.app.state.auditor.record(
        "notification.drained", actor="system", resource="notifications",
        ip=request.client.host if request.client else "",
        metadata={k: str(v) for k, v in result.items() if isinstance(v, (int, str))})
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
    """Erase a data subject. Called by compliance-service during a DSAR fan-out.

    Returns the ack: what was erased, and what was RETAINED with its legal basis. Idempotent —
    a DSAR retries, so erasing an already-erased subject must not fail.
    """
    # Write-ahead: a critical action the log cannot take must not happen at all, so this
    # runs BEFORE the mutation. Recording afterwards would 503 a caller whose action had
    # already succeeded, and leave it unrecorded.
    await request.app.state.auditor.record(
        "subject.erased", actor="", tenant=body.tenant, resource=f"subject:{body.subject}",
        ip=request.client.host if request.client else "")
    c = request.app.state.container
    s = get_settings()
    eraser = NotificationErasure(store=c.datastore, pii_salt=s.PII_SALT)
    return await eraser.erase(body.tenant, body.subject)


@router.post("/whatsapp/templates/sync", dependencies=[Depends(require_internal_token)])
async def sync_whatsapp_templates(request: Request, tenant: str,
                                  settings: Settings = Depends(get_settings)) -> dict:
    """Reconcile a tenant's pending WhatsApp templates against Meta's approval verdict.

    Driven by the platform scheduler (per tenant), not a thread here — a status change is a durable
    fact written down, so an operator sees approved/rejected without re-asking Meta."""
    from app.core.whatsapp_templates import WhatsAppTemplateRegistry, WhatsAppTemplateStore
    c = request.app.state.container
    reg = WhatsAppTemplateRegistry(WhatsAppTemplateStore(c.datastore))
    return await reg.sync(tenant=tenant, manager=c.whatsapp_template_manager,
                          limit=settings.WHATSAPP_TEMPLATE_SYNC_LIMIT)


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
