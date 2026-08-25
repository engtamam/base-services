# =============================================================================
# notification-service — Internal tier (service-to-service, X-Internal-Token)
# -----------------------------------------------------------------------------
# NOT user-facing. Guarded by a shared token compared in CONSTANT TIME
# (CWE-208: observable timing discrepancy). Never a user JWT.
# =============================================================================
from __future__ import annotations

import hmac
from typing import Literal

from fastapi import APIRouter, Depends, Header, HTTPException, Query, Request, status
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
    # WhatsApp only: an arbitrary caller string (≤512) Meta echoes verbatim in the status webhook, so a
    # caller can correlate a receipt against its OWN send id with no join and no extra read.
    biz_opaque_callback_data: str | None = Field(default=None, max_length=512)
    # WhatsApp only: which of the tenant's NUMBERS to send from — a WABA holds many (one per branch).
    # It is the branch's phone-number-id. Resolved to a number this tenant is entitled to send from;
    # an UNKNOWN identity is REFUSED (a message from another branch's number is worse than none sent),
    # never quietly resolved to the tenant default. Absent = the tenant's default number.
    sending_identity: str | None = Field(default=None, max_length=128)

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


async def _resolve_sending_identity(request: Request, tenant: str, sending_identity: str | None):
    """Resolve a per-branch sending identity to the number this tenant is ENTITLED to send from.

    Returns (override_provider, None) — override is None when no identity was requested (use the
    tenant default) — or (None, JSONResponse-409) when the identity is not this tenant's. It is
    REFUSED, never quietly resolved to the default number, because a message from the wrong branch's
    number is worse than one not sent. Shared by /notify and /notify/whatsapp/interactive.
    """
    if not sending_identity:
        return None, None
    from starlette.responses import JSONResponse
    pool = getattr(request.app.state, "provider_pool", None)
    override = (await pool.whatsapp_for_identity(tenant or "", sending_identity)
                if pool is not None else None)
    if override is None:
        return None, JSONResponse({"ok": False, "reason": "UNKNOWN_SENDING_IDENTITY"}, status_code=409)
    return override, None


@router.post("/notify", dependencies=[Depends(require_internal_token)])
async def internal_notify(request: Request, body: NotifyIn) -> dict:
    """Service-to-service send. Returns the delivery id + status."""
    # body.tenant, not getattr(body, 'tenant', ''): the defensive access is what turned a missing
    # field from a loud AttributeError on the first request into a silent wrong answer. And the
    # tenant must reach .notify() too — build_notifier's tenant only scopes the provider pool, while
    # the consent gate reads notify()'s own tenant argument.
    whatsapp_override, refusal = await _resolve_sending_identity(
        request, body.tenant or "", body.sending_identity)
    if refusal is not None:
        return refusal
    return await (await build_notifier(request, body.tenant or "")).notify(
        recipient=body.recipient, channel=body.channel, template_key=body.template_key,
        tenant=body.tenant, locale=body.locale, variables=body.variables,
        user_id=body.user_id, event=body.event, idempotency_key=body.idempotency_key,
        biz_opaque_callback_data=body.biz_opaque_callback_data, whatsapp_override=whatsapp_override,
    )


class _ButtonIn(BaseModel):
    id: str = Field(min_length=1, max_length=256)     # echoed back in the reply webhook — the anchor
    title: str = Field(min_length=1, max_length=256)  # builder enforces Meta's 20-char cap loudly


class _RowIn(BaseModel):
    id: str = Field(min_length=1, max_length=256)
    title: str = Field(min_length=1, max_length=256)
    description: str = Field(default="", max_length=256)


class _SectionIn(BaseModel):
    title: str = Field(default="", max_length=256)
    rows: list[_RowIn] = Field(min_length=1)


class InteractiveNotifyIn(BaseModel):
    recipient: str = Field(min_length=1, max_length=256)
    tenant: str | None = Field(default=None, max_length=128)
    type: Literal["button", "list"]
    body: str = Field(min_length=1, max_length=1024)
    header: str = Field(default="", max_length=1024)
    footer: str = Field(default="", max_length=1024)
    # button messages:
    buttons: list[_ButtonIn] = Field(default_factory=list)
    # list messages:
    button_text: str = Field(default="", max_length=256)
    sections: list[_SectionIn] = Field(default_factory=list)
    biz_opaque_callback_data: str | None = Field(default=None, max_length=512)
    sending_identity: str | None = Field(default=None, max_length=128)
    idempotency_key: str | None = Field(default=None, max_length=256)


@router.post("/notify/whatsapp/interactive", dependencies=[Depends(require_internal_token)])
async def internal_notify_interactive(request: Request, body: InteractiveNotifyIn) -> dict:
    """Send an interactive (reply-buttons / list) WhatsApp message — the state-change-safe path.

    An interactive message drives an action by the customer TAPPING a button/list row, whose id
    returns in a webhook — never a free-text or LLM-inferred action. It is sent inside the 24h
    customer-service window (no template needed), and goes through the SAME audited, rate-limited,
    ceiling-aware plumbing as every other WhatsApp send (a tenant-scoped Delivery carrying the wamid),
    so a caller need not reach the Cloud API directly. The interactive object is built and
    limit-checked here (core/whatsapp_interactive) so a bad spec is a clear 422, not a Meta 400.
    """
    from app.core.whatsapp_interactive import (InteractiveError, build_button, build_list)

    override, refusal = await _resolve_sending_identity(
        request, body.tenant or "", body.sending_identity)
    if refusal is not None:
        return refusal
    try:
        if body.type == "button":
            interactive = build_button(body=body.body, header=body.header, footer=body.footer,
                                       buttons=[(b.id, b.title) for b in body.buttons])
        else:
            interactive = build_list(
                body=body.body, header=body.header, footer=body.footer, button_text=body.button_text,
                sections=[{"title": s.title, "rows": [r.model_dump() for r in s.rows]}
                          for s in body.sections])
    except InteractiveError as exc:
        # A caller-fixable spec error (a fourth button, an over-length title). The reason is not
        # sensitive and a machine caller needs it to correct the spec, so return it in the body —
        # the same {ok:false, reason} shape the sending-identity refusal uses — rather than let the
        # global handler collapse it to a bare VALIDATION_ERROR.
        from starlette.responses import JSONResponse
        return JSONResponse({"ok": False, "reason": str(exc)}, status_code=422)
    notifier = await build_notifier(request, body.tenant or "")
    return await notifier.send_interactive(
        recipient=body.recipient, tenant=body.tenant or "", interactive=interactive, kind=body.type,
        biz_opaque=body.biz_opaque_callback_data or "", idempotency_key=body.idempotency_key,
        whatsapp_override=override)


class EmbeddedSignupInternalIn(BaseModel):
    """Same shape as the operator route, but the TENANT rides in the body — the internal caller is a
    product service acting FOR a shop, not a human whose token carries a tenant."""
    tenant: str = Field(default="", max_length=128)
    code: str = Field(..., min_length=1, max_length=4096)
    waba_id: str = Field(..., min_length=1, max_length=128)
    phone_number_id: str = Field(..., min_length=1, max_length=128)
    # Optional: when the business supplies its 6-digit PIN at connect, the number is REGISTERED in the
    # same flow (a mute "connected" number is the failure this avoids). Validated shape; sealed on
    # success. Absent => the token is stored and the standalone /whatsapp/register finishes it later.
    pin: str | None = Field(default=None, pattern=r"^[0-9]{6}$")


@router.post("/whatsapp/embedded-signup", dependencies=[Depends(require_internal_token)])
async def internal_embedded_signup(request: Request, body: EmbeddedSignupInternalIn) -> dict:
    """The internal twin of POST /api/v1/operator/whatsapp/embedded-signup.

    WHY IT EXISTS: the exchange needs platform.secrets, which a SHOP OWNER must never hold — it reads
    and writes every establishment's credentials. But only the owner's browser ever sees the ~30s
    authorization code. So a product service the owner IS authenticated to receives the code and
    forwards it here with X-Internal-Token + the tenant, and the exchange runs under the internal
    credential instead of a human capability. No new logic — the SAME WhatsAppOnboarding.exchange:
    the token never leaves core, the audit row names the WABA, the pool is invalidated. Adopting it
    is one values flip on the product side (WHATSAPP_ONBOARDING_DRIVER: http)."""
    from app.api.v1.operator import _invalidate, _secret_store
    from app.core.whatsapp_onboarding import OnboardingError, WhatsAppOnboarding
    from app.ports.errors import ConfigError

    s = get_settings()
    await request.app.state.auditor.record(
        "operator.whatsapp_onboarded", actor="", tenant=body.tenant or "",
        resource=f"waba:{body.waba_id}",          # the WABA id, never the code or the token
        ip=request.client.host if request.client else "")
    onboarding = WhatsAppOnboarding(
        _secret_store(request),
        app_id=s.META_APP_ID, app_secret=s.META_APP_SECRET,
        graph_base=s.WHATSAPP_ONBOARDING_GRAPH_BASE,
        on_stored=lambda t: _invalidate(request, t))
    try:
        result = await onboarding.exchange(
            tenant=body.tenant, code=body.code, waba_id=body.waba_id,
            phone_number_id=body.phone_number_id, pin=body.pin or "")
    except ConfigError as exc:
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, str(exc)) from exc
    except OnboardingError as exc:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, str(exc)) from exc
    if result.get("ok"):
        # Bind number → tenant so an operator (or later an inbound event keyed only by number) can
        # reach this shop's deliveries. Best-effort: the credentials ARE stored, so a failure here
        # must not turn onboarding into an error — the map is rebuilt on the next registration.
        try:
            from app.core.stores import WhatsAppNumberIndex
            await WhatsAppNumberIndex(request.app.state.container.datastore).bind(
                body.phone_number_id, body.tenant)
        except Exception:  # noqa: BLE001
            pass
    return result


@router.get("/notifications/receipts", dependencies=[Depends(require_internal_token)])
async def delivery_receipts(request: Request, after: str = "", limit: int = 200) -> dict:
    """The catch-up read: delivery/read receipts forward from a cursor, oldest first, bounded.

    The status bus is at-most-once — a pod that restarts through a receipt hears it never — so this is
    how a consumer reconciles: read forward, persist the returned `cursor` as its watermark, repeat.
    Each row carries delivery_id + provider_message_id, so a receipt is matched to the send it belongs
    to with no guesswork. Ordered by receipt_ts; a receipt that only lived on the bus is still here."""
    from app.core.stores import DeliveryStore
    store = DeliveryStore(request.app.state.container.datastore)
    rows, cursor = await store.changed_since(after=after or None, limit=max(1, min(1000, limit)))
    return {"receipts": rows, "cursor": cursor}


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
    # The KIND of identifier `subject` is (pseudonym, phone_hash, internal_id). Optional; "" = today's
    # behaviour. When set and this service indexes by a DIFFERENT kind, the request is refused
    # explicitly rather than matched against nothing — see the handler.
    subject_kind: str = Field(default="", max_length=64)


@router.post("/privacy/erase", dependencies=[Depends(require_internal_token)])
async def erase_subject(request: Request, body: EraseIn) -> dict:
    """Erase a data subject. Called by compliance-service during a DSAR fan-out.

    Returns the ack: what was erased, and what was RETAINED with its legal basis. Idempotent —
    a DSAR retries, so erasing an already-erased subject must not fail.
    """
    s = get_settings()
    # EXPLICIT REFUSAL of an identifier kind we do not index — BEFORE the write-ahead audit, since a
    # refusal is not an erasure and must not be logged as one. Matching a phone_hash against a store
    # keyed by internal_id yields zero rows and a false "erased"; we say unrecognized_subject_kind
    # instead and the dispatcher classifies it FAILED, keeping the DSAR open. Default = accept any kind.
    indexed = s.privacy_indexed_subject_kinds
    if body.subject_kind and indexed and body.subject_kind not in indexed:
        return {"service": s.SERVICE_NAME, "erased": [], "retained": [], "already_absent": False,
                "unrecognized_subject_kind": True, "subject_kind": body.subject_kind,
                "indexed_kinds": indexed}
    # Write-ahead: a critical action the log cannot take must not happen at all, so this
    # runs BEFORE the mutation. Recording afterwards would 503 a caller whose action had
    # already succeeded, and leave it unrecorded.
    await request.app.state.auditor.record(
        "subject.erased", actor="", tenant=body.tenant, resource=f"subject:{body.subject}",
        ip=request.client.host if request.client else "")
    c = request.app.state.container
    eraser = NotificationErasure(store=c.datastore, pii_salt=s.PII_SALT)
    return await eraser.erase(body.tenant, body.subject)


@router.post("/whatsapp/templates/sync", dependencies=[Depends(require_internal_token)])
async def sync_whatsapp_templates(request: Request, tenant: str,
                                  settings: Settings = Depends(get_settings)) -> dict:
    """Reconcile a tenant's live WhatsApp templates against Meta's approval verdict.

    Driven by the platform scheduler (per tenant), not a thread here — a status change is a durable
    fact written down, so an operator sees approved/rejected/paused/disabled without re-asking Meta.
    The manager is resolved per tenant so the verdict comes from the tenant's own WABA."""
    from app.api.v1.notifications import build_template_manager
    from app.core.whatsapp_templates import WhatsAppTemplateRegistry, WhatsAppTemplateStore
    c = request.app.state.container
    reg = WhatsAppTemplateRegistry(WhatsAppTemplateStore(c.datastore))
    return await reg.sync(tenant=tenant, manager=await build_template_manager(request, tenant),
                          limit=settings.WHATSAPP_TEMPLATE_SYNC_LIMIT)


class WaTemplateInternalIn(BaseModel):
    """Same shape as POST /api/v1/whatsapp/templates, but the TENANT rides in the body — the caller
    is a product service acting FOR a shop (an account just connected, a scheduled library upload),
    not a human whose token carries a tenant."""
    tenant: str = Field(min_length=1, max_length=128)
    name: str = Field(min_length=1, max_length=512)
    language: str = Field(min_length=2, max_length=16)
    category: str = Field(min_length=1, max_length=32)
    body: str = Field(default="", max_length=4_000)
    variables: list[str] = Field(default_factory=list)   # ordered parameter names (send-time keys)
    examples: list[str] = Field(default_factory=list)     # ordered example values Meta needs at review


class WaSubmitInternalIn(BaseModel):
    tenant: str = Field(min_length=1, max_length=128)


@router.post("/whatsapp/templates", status_code=201, dependencies=[Depends(require_internal_token)])
async def register_whatsapp_template_internal(request: Request, body: WaTemplateInternalIn) -> dict:
    """The internal twin of POST /api/v1/whatsapp/templates.

    WHY IT EXISTS: registering the library is NOT a user action — it belongs to the moment an account
    connects, and Meta rate-limits it, so it is a backend job, not a browser doing 132 calls that stop
    when the tab closes. The user route needs a JWT whose signing key must stay in identity-service;
    a service holds no session. So this is the token-authed counterpart with the tenant in the body —
    the SAME registry.register() upsert, INCLUDING the guard that a no-op re-register never resets an
    approved template back to pending (whatsapp_templates.register)."""
    from app.core.whatsapp_templates import (TemplateError, WhatsAppTemplateRegistry,
                                             WhatsAppTemplateStore)
    await request.app.state.auditor.record(
        "whatsapp_template.registered", actor="", tenant=body.tenant,
        resource=f"wa_template:{body.name}", ip=request.client.host if request.client else "",
        metadata={"category": body.category, "language": body.language})
    reg = WhatsAppTemplateRegistry(WhatsAppTemplateStore(request.app.state.container.datastore))
    try:
        t = await reg.register(tenant=body.tenant, name=body.name, language=body.language,
                               category=body.category, body=body.body, variables=body.variables,
                               examples=body.examples)
    except TemplateError as exc:
        raise HTTPException(422, str(exc)) from exc
    return t.public()


@router.post("/whatsapp/templates/{name}/{language}/submit",
             dependencies=[Depends(require_internal_token)])
async def submit_whatsapp_template_internal(request: Request, name: str, language: str,
                                            body: WaSubmitInternalIn) -> dict:
    """The internal twin of POST /api/v1/whatsapp/templates/{name}/{language}/submit — hand ONE
    registered template to Meta for review. Same registry.submit, tenant in the body. For draining a
    whole freshly-registered library within Meta's rate limit, prefer submit-pending below."""
    from app.api.v1.notifications import build_template_manager
    from app.core.whatsapp_templates import (TemplateError, WhatsAppTemplateRegistry,
                                             WhatsAppTemplateStore)
    await request.app.state.auditor.record(
        "whatsapp_template.submitted", actor="", tenant=body.tenant,
        resource=f"wa_template:{name}", ip=request.client.host if request.client else "")
    reg = WhatsAppTemplateRegistry(WhatsAppTemplateStore(request.app.state.container.datastore))
    try:
        t = await reg.submit(tenant=body.tenant, name=name, language=language,
                             manager=await build_template_manager(request, body.tenant))
    except TemplateError as exc:
        raise HTTPException(404, str(exc)) from exc
    return t.public()


@router.post("/whatsapp/templates/submit-pending", dependencies=[Depends(require_internal_token)])
async def submit_pending_whatsapp_templates_internal(
        request: Request, tenant: str, settings: Settings = Depends(get_settings)) -> dict:
    """Drain a tenant's never-submitted templates to Meta, within its 100/hour cap.

    This is the backend job the escalation asked for: "automatic library upload on connect" without a
    browser and without any caller reimplementing Meta's throttle. Register the library once (loop the
    register twin — it is a local upsert, no Meta call, no limit), then let the platform scheduler tick
    this hourly per tenant until the library is through — the same per-tenant cron pattern as
    /whatsapp/templates/sync:

        cronJobs:
          - name: notify-wa-template-drain
            schedule: "7 * * * *"
            path: /internal/whatsapp/templates/submit-pending?tenant=<tenant>

    Idempotent: it submits only pending templates with NO Meta id (never handed over), so a re-tick
    never double-submits one already awaiting a verdict."""
    if not tenant:
        raise HTTPException(400, "tenant is required")
    from app.api.v1.notifications import build_template_manager
    from app.core.whatsapp_templates import WhatsAppTemplateRegistry, WhatsAppTemplateStore
    reg = WhatsAppTemplateRegistry(WhatsAppTemplateStore(request.app.state.container.datastore))
    result = await reg.submit_pending(
        tenant=tenant, manager=await build_template_manager(request, tenant),
        limit=settings.WHATSAPP_TEMPLATE_SUBMIT_LIMIT, scan_limit=settings.WHATSAPP_TEMPLATE_SYNC_LIMIT)
    # Recorded AFTER: submit-pending is a batch of individually-audited submits' net effect; the row
    # names counts, never a person, and an empty batch is a legitimate no-op worth seeing.
    await request.app.state.auditor.record(
        "whatsapp_template.submitted", actor="system", tenant=tenant,
        resource="whatsapp_templates", ip=request.client.host if request.client else "",
        metadata={k: str(result[k]) for k in ("submitted", "failed", "remaining")})
    return result


class WaRegisterIn(BaseModel):
    tenant: str = Field(min_length=1, max_length=128)
    # PER NUMBER, not per tenant: a WABA can hold many numbers (a number per branch for a chain) and
    # registration is per number. Optional — omitted falls back to the tenant's single stored number,
    # for the single-brand case.
    phone_number_id: str | None = Field(default=None, max_length=128)
    # Meta's 6-digit PIN, validated HERE so a malformed PIN never spends one of the 10 attempts Meta
    # allows before it locks the number for 72 hours. OPTIONAL: omitted, the sealed PIN for this number
    # is used, so a re-registration (required after some account events) needs no human to re-enter it.
    pin: str | None = Field(default=None, pattern=r"^[0-9]{6}$")


@router.post("/whatsapp/register", dependencies=[Depends(require_internal_token)])
async def register_whatsapp_number(request: Request, body: WaRegisterIn):
    """Register a business phone number for Cloud API sending — the PIN-carrying step Embedded Signup
    stops before. Meta refuses to SEND until a number is registered (error 133010), so an unregistered
    number is "connected" and mute.

    PER NUMBER: the token is the WABA's (per tenant); the number is `phone_number_id` (a chain has
    many). The PIN may be supplied, or read from the sealed store for a re-registration. Meta's verdict
    is returned as three distinct sentences, never one 400: WRONG_PIN (mistyped), RATE_LIMITED (the
    10-per-72h lock, error 133016 — WAIT, do not retry), UNREACHABLE. NOT_CONNECTED = no token stored
    (finish Embedded Signup); NO_PIN = no PIN supplied and none sealed."""
    import asyncio

    from starlette.responses import JSONResponse

    from app.core.whatsapp_register import pin_secret_name, register_number

    s = get_settings()
    pool = getattr(request.app.state, "provider_pool", None)
    if pool is None:
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, "PROVIDER_POOL_UNAVAILABLE")
    token = await pool.whatsapp_access_token(body.tenant)
    # The specified number, or the tenant's single stored one (single-brand fallback).
    phone_id = body.phone_number_id or await pool.whatsapp_phone_number_id(body.tenant)
    if not token or not phone_id:
        # 409, NOT 400 — the SAME NOT_CONNECTED contract that /whatsapp/status returns 409 for. A
        # missing stored token/phone is a state CONFLICT (the shop has not finished Embedded Signup),
        # not a malformed request; the two sibling endpoints used to disagree (400 here, 409 there),
        # forcing every consumer to special-case one and mis-reading a refusal from the wrong sibling.
        return JSONResponse({"registered": False, "reason": "NOT_CONNECTED"}, status_code=409)
    # Supplied PIN, else the one sealed for THIS number — so a re-registration repeats without a human.
    pin = body.pin or await pool.whatsapp_pin(body.tenant, phone_id)
    if not pin:
        # Also a precondition/state conflict (no PIN supplied and none sealed), not a syntax error → 409.
        return JSONResponse({"registered": False, "reason": "NO_PIN"}, status_code=409)
    res = await asyncio.to_thread(       # off the loop: a blocking Graph POST must not stall it
        register_number, access_token=token, phone_number_id=phone_id, pin=pin,
        graph_base=s.WHATSAPP_ONBOARDING_GRAPH_BASE)
    if res.registered and body.pin:
        # Seal a freshly-supplied PIN (only on success, only when the caller gave one), so the next
        # re-registration needs no human. A sealed value is never returned by any read. BEST-EFFORT:
        # the number IS registered at Meta — a store that cannot seal the PIN must not turn that
        # success into a failure; the PIN is simply re-supplied next time.
        try:
            from app.api.v1.operator import _secret_store
            await _secret_store(request).set(tenant=body.tenant, name=pin_secret_name(phone_id),
                                             value=pin, actor_ref="")
        except Exception:  # noqa: BLE001 — sealing is a convenience, registration already succeeded
            pass
    if res.registered:
        # Bind number → tenant here too — registration is the other place a number becomes a tenant's.
        try:
            from app.core.stores import WhatsAppNumberIndex
            await WhatsAppNumberIndex(request.app.state.container.datastore).bind(phone_id, body.tenant)
        except Exception:  # noqa: BLE001 — best-effort, the number is registered regardless
            pass
    await request.app.state.auditor.record(
        "whatsapp.number_registered", actor="", tenant=body.tenant,
        resource=f"phone:{phone_id}", ip=request.client.host if request.client else "",
        metadata={"registered": str(res.registered), "reason": res.reason})
    if res.registered:
        return {"registered": True}
    return JSONResponse({"registered": False, "reason": res.reason}, status_code=400)


@router.get("/whatsapp/status", dependencies=[Depends(require_internal_token)])
async def whatsapp_number_status(request: Request, tenant: str, phone_number_id: str = ""):
    """A number's REAL readiness, read live from Meta — so a shop's state is READABLE, not assumed.

    Returns platform_type (native CLOUD_API vs a coexistence number, which stops after ~14 days of
    primary-device inactivity), code_verification_status, throughput and quality_rating. Per number:
    `phone_number_id` selects one of a chain's numbers, or falls back to the tenant's single number."""
    if not tenant:
        raise HTTPException(400, "tenant is required")
    import asyncio

    from app.core.whatsapp_register import fetch_status
    s = get_settings()
    pool = getattr(request.app.state, "provider_pool", None)
    if pool is None:
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, "PROVIDER_POOL_UNAVAILABLE")
    from starlette.responses import JSONResponse
    token = await pool.whatsapp_access_token(tenant)
    phone_id = phone_number_id or await pool.whatsapp_phone_number_id(tenant)
    if not token or not phone_id:
        # ONE rule across the internal API: a refusal is a NON-2xx, never {ok:false} on a 200 that a
        # status-code-only caller reads as success — which is exactly how a shop stayed "connected"
        # while sending nothing. The body still carries the reason for a caller that reads it.
        return JSONResponse({"ok": False, "reason": "NOT_CONNECTED"}, status_code=409)
    res = await asyncio.to_thread(fetch_status, access_token=token, phone_number_id=phone_id,
                                  graph_base=s.WHATSAPP_ONBOARDING_GRAPH_BASE)
    if not res.get("ok"):
        # We could not read the number's state from Meta (unreachable, a rejection, a revoked token).
        # That is a failed read, not a status of "not ready" — answer 502 so a caller does not record
        # a live number as broken; TOKEN_REVOKED especially must reach a disconnect sweep as a failure.
        return JSONResponse(res, status_code=status.HTTP_502_BAD_GATEWAY)
    return res


# Fields a delivery diagnostic exposes. A whitelist, and deliberately WITHOUT the raw recipient: the
# operator needs to correlate a failed message, not a customer's full phone number, so the recipient
# is masked to its last four. Everything else is the send/receipt state the "why is this broken"
# screen reasons about — our status machine, Meta's receipt axis, and the failure reason.
def _delivery_diag(row: dict) -> dict:
    recipient = row.get("recipient", "") or ""
    hint = ("•••" + recipient[-4:]) if len(recipient) > 4 else ("•" * len(recipient))
    return {"id": row.get("id", ""), "recipient_hint": hint,
            "template_key": row.get("template_key", ""), "status": row.get("status", ""),
            "error": row.get("error", ""), "provider": row.get("provider", ""),
            "provider_message_id": row.get("provider_message_id", ""),
            "receipt_status": row.get("receipt_status", ""),
            "receipt_ts": row.get("receipt_ts", 0.0), "attempts": row.get("attempts", 0),
            "ts": row.get("ts", "")}


@router.get("/whatsapp/deliveries", dependencies=[Depends(require_internal_token)])
async def whatsapp_deliveries(request: Request, tenant: str = Query(default="", max_length=128),
                              phone_number_id: str = Query(default="", max_length=128),
                              limit: int = Query(default=50, ge=1, le=500)) -> dict:
    """Recent WhatsApp deliveries for ONE shop — the 'why is this shop's WhatsApp broken' view.

    Addressed by `tenant`, or by the shop's `phone_number_id` resolved through the number→tenant
    index (the operator usually has the number, not the internal tenant id). Tenant-scoped by design:
    the query is pushed into the datastore keyed on tenant + channel, so it reads one shop's rows and
    never becomes a cross-tenant dump. Each row carries our send status, Meta's receipt status, and
    the failure reason — with the recipient masked to its last four digits.
    """
    from app.core.stores import DeliveryStore, WhatsAppNumberIndex
    ds = request.app.state.container.datastore
    resolved = tenant
    if not resolved and phone_number_id:
        resolved = await WhatsAppNumberIndex(ds).tenant_of(phone_number_id) or ""
    if not resolved:
        # No tenant, and no number we recognise: refuse rather than answer an unfiltered scan.
        raise HTTPException(status.HTTP_400_BAD_REQUEST,
                            "a tenant, or a known phone_number_id, is required")
    rows = await DeliveryStore(ds).by_tenant(resolved, limit, channel="whatsapp")
    return {"tenant": resolved, "deliveries": [_delivery_diag(r) for r in rows]}


@router.get("/whatsapp/messaging-limit", dependencies=[Depends(require_internal_token)])
async def whatsapp_messaging_limit_internal(request: Request, tenant: str = "") -> dict:
    """This tenant's WhatsApp messaging limit (unique customers / 24h) + quality rating, read live
    from Meta and briefly cached. The one number a sending service needs BEFORE the send to reserve
    the day's capacity, tell the owner where he stands, and size a campaign — instead of learning the
    ceiling from Meta's rejection after it is already spent."""
    if not tenant:
        raise HTTPException(400, "tenant is required")
    from app.api.v1.notifications import whatsapp_messaging_limit
    return await whatsapp_messaging_limit(request, tenant)


@router.get("/whatsapp/templates", dependencies=[Depends(require_internal_token)])
async def list_whatsapp_templates_internal(request: Request, tenant: str = "", status: str = "",
                                           limit: int = 100) -> dict:
    """Which templates are approved (or pending/paused/…) for this tenant — for a PEER SERVICE.

    A sending service that picks a template by TYPE needs to prefer an APPROVED one before it spends
    a send and a slot in the daily cap on a template notify() will refuse. The user-tier list needs a
    JWT (whose signing key must stay in identity-service) and caching the statuses is a second copy
    of another service's state that drifts on the first sync — so this is the internal, token-authed
    counterpart to GET /api/v1/whatsapp/templates: same handler, tenant as a parameter."""
    if not tenant:
        # 400 as a literal, not status.HTTP_400_*: the `status` QUERY PARAM below shadows the
        # imported status module inside this function.
        raise HTTPException(400, "tenant is required")
    from app.core.whatsapp_templates import WhatsAppTemplateRegistry, WhatsAppTemplateStore
    reg = WhatsAppTemplateRegistry(WhatsAppTemplateStore(request.app.state.container.datastore))
    rows, cursor = await reg.list(tenant, status=status, limit=max(1, min(200, limit)))
    return {"templates": rows, "cursor": cursor}


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
