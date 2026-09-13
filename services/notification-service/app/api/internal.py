# =============================================================================
# notification-service — Internal tier (service-to-service, X-Internal-Token)
# -----------------------------------------------------------------------------
# NOT user-facing. Guarded by a shared token compared in CONSTANT TIME
# (CWE-208: observable timing discrepancy). Never a user JWT.
# =============================================================================
from __future__ import annotations

import asyncio
from typing import Any, Literal

from fastapi import APIRouter, Depends, Header, HTTPException, Query, Request, status
from pydantic import BaseModel, Field

from app.api.v1.notifications import build_notifier
from app.backup_manifest import BACKUP
from app.core import backup as _backup
from app.config import Settings, get_settings
from app.core.erasure import NotificationErasure
from app.core.pii import subject_ref   # used at module-function scope; MUST be a module-level import
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
    # WhatsApp TEMPLATE-with-Flow-button only: when the approved template carries a Flow button, these
    # supply the per-send action. flow_token is the reply anchor (echoed in the completion webhook);
    # flow_action_data seeds the first screen (omitted when empty). Both ignored for ordinary templates.
    flow_token: str | None = Field(default=None, max_length=256)
    flow_action_data: dict | None = Field(default=None)
    # WhatsApp only: an image header on an ordinary (single-message) template — a media_id already
    # uploaded via /internal/whatsapp/media/upload-id. Builds the same header component the carousel
    # path builds per card, for a text+image+button campaign message that needs no carousel. Ignored
    # for non-WhatsApp channels and for templates with no header. Declared only when supplied.
    header_media_id: str | None = Field(default=None, max_length=256)
    # WhatsApp only: a per-send PAYLOAD for a quick-reply button that is declared+APPROVED on the
    # template (the button is fixed; which service the customer tapped varies each send). Builds the
    # same button component the carousel path builds per card. Ignored for non-WhatsApp channels and
    # for templates with no quick-reply button. Declared only when supplied.
    button_payload: str | None = Field(default=None, max_length=256)
    # WhatsApp only: which of the tenant's NUMBERS to send from — a WABA holds many (one per branch).
    # It is the branch's phone-number-id. Resolved to a number this tenant is entitled to send from;
    # an UNKNOWN identity is REFUSED (a message from another branch's number is worse than none sent),
    # never quietly resolved to the tenant default. Absent = the tenant's default number.
    sending_identity: str | None = Field(default=None, max_length=128)

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


@router.get("/whatsapp/flow/{name}-json", dependencies=[Depends(require_internal_token)])
async def flow_json_for(request: Request, name: str) -> dict:
    """The companion Flow JSON for ANY registered slot-reservation Flow, keyed by its engine name
    (`booking`, `delivery`, `visit`, …) — one route instead of one per Flow forever. A CONVENIENCE
    artifact the platform uploads to Meta ITS OWN WAY (we never call Meta). The screens/field names are
    generated from the same constants the engine reads, so they cannot drift; validate on upload (Meta
    revises the Flow JSON schema per version).

    404 when no engine is registered under that name, when the engine produces no Flow JSON
    (loyalty/order_status), or when a booking engine has no definition configured. Booking-style engines
    (booking/delivery/visit) AND the reschedule engine are previewable."""
    from app.core.flow_json import FlowPreviewUnavailable, build_flow_preview_json
    registry = getattr(request.app.state, "flow_registry", None)
    handler = registry.get(name) if registry is not None else None
    try:
        return await build_flow_preview_json(handler)
    except FlowPreviewUnavailable as exc:
        raise HTTPException(404, str(exc)) from exc


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
        flow_token=body.flow_token, flow_action_data=body.flow_action_data,
        header_media_id=body.header_media_id, button_payload=body.button_payload,
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
    type: Literal["button", "list", "flow", "location_request"]
    body: str = Field(min_length=1, max_length=1024)
    header: str = Field(default="", max_length=1024)
    footer: str = Field(default="", max_length=1024)
    # button messages:
    buttons: list[_ButtonIn] = Field(default_factory=list)
    # list messages:
    button_text: str = Field(default="", max_length=256)
    sections: list[_SectionIn] = Field(default_factory=list)
    # flow messages — a form that opens inside WhatsApp (see core/whatsapp_interactive.build_flow).
    # `flow_token` is the reply anchor, same role as a button's own `id`.
    flow_token: str = Field(default="", max_length=256)
    # Identify the flow by EITHER flow_id OR flow_name (exactly one). flow_name is the WABA-portable
    # id: a Flow cloned per tenant keeps its name but gets a new id, so name works across every tenant.
    flow_id: str = Field(default="", max_length=128)
    flow_name: str = Field(default="", max_length=128)
    flow_cta: str = Field(default="", max_length=20)
    # "navigate" (static flow, flow_screen required) or "data_exchange" (dynamic, endpoint-driven —
    # e.g. the booking calendar; the endpoint's INIT supplies the first screen so flow_screen is
    # optional). Defaults to navigate so existing static-flow sends are unchanged.
    flow_action: str = Field(default="navigate", max_length=16)
    flow_screen: str = Field(default="", max_length=128)
    flow_data: dict[str, Any] = Field(default_factory=dict)
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
    from app.core.whatsapp_interactive import (InteractiveError, build_button, build_flow,
                                               build_list, build_location_request)

    override, refusal = await _resolve_sending_identity(
        request, body.tenant or "", body.sending_identity)
    if refusal is not None:
        return refusal
    try:
        if body.type == "button":
            interactive = build_button(body=body.body, header=body.header, footer=body.footer,
                                       buttons=[(b.id, b.title) for b in body.buttons])
        elif body.type == "flow":
            interactive = build_flow(
                body=body.body, header=body.header, footer=body.footer,
                flow_token=body.flow_token, flow_id=body.flow_id, flow_name=body.flow_name,
                flow_cta=body.flow_cta, screen=body.flow_screen, data=body.flow_data,
                flow_action=body.flow_action)
        elif body.type == "location_request":
            # A location-request carries only body text; header/footer/action fields are not accepted
            # by Meta for this interactive type, so they are ignored rather than forwarded.
            interactive = build_location_request(body=body.body)
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


class _CarouselCardIn(BaseModel):
    media_id: str = Field(min_length=1, max_length=256)         # from /whatsapp/media/upload-id
    media_type: Literal["image", "video"] = "image"
    body_params: list[str] = Field(default_factory=list)       # values for the card body's {{n}}
    # buttons: [{sub_type: quick_reply, payload} | {sub_type: url, text}] — order matches the template
    buttons: list[dict] = Field(default_factory=list)


class CarouselNotifyIn(BaseModel):
    recipient: str = Field(min_length=1, max_length=256)
    tenant: str | None = Field(default=None, max_length=128)
    template_name: str = Field(min_length=1, max_length=512)   # the approved carousel template
    language: str = Field(default="en", min_length=2, max_length=16)
    variables: list[str] = Field(default_factory=list)         # top-level (non-card) body params
    cards: list[_CarouselCardIn] = Field(min_length=1)
    user_id: str | None = Field(default=None, max_length=256)  # subject for the consent gate
    event: str | None = Field(default=None, max_length=128)    # maps to a consent purpose, if any
    biz_opaque_callback_data: str | None = Field(default=None, max_length=512)
    sending_identity: str | None = Field(default=None, max_length=128)
    idempotency_key: str | None = Field(default=None, max_length=256)


@router.post("/notify/whatsapp/carousel", dependencies=[Depends(require_internal_token)])
async def internal_notify_carousel(request: Request, body: CarouselNotifyIn) -> dict:
    """Send an approved CAROUSEL template with per-card runtime values.

    Business-initiated (a template, not a 24h-window reply), so it goes through the SAME audited,
    idempotent, tenant-scoped, CONSENT-GATED plumbing as any other template send — a carousel mapped
    to a marketing purpose is not sent to a subject who has not consented. Each card's media is
    referenced by the media id from /whatsapp/media/upload-id; the component tree is built + validated
    here (a bad spec is a clean 422, not a Meta 400)."""
    from app.core.whatsapp_carousel import CarouselSendError, build_carousel_send

    override, refusal = await _resolve_sending_identity(
        request, body.tenant or "", body.sending_identity)
    if refusal is not None:
        return refusal
    try:
        components: list[dict] = []
        if body.variables:
            components.append({"type": "body",
                               "parameters": [{"type": "text", "text": str(v)} for v in body.variables]})
        components.append(build_carousel_send([c.model_dump() for c in body.cards]))
    except CarouselSendError as exc:
        from starlette.responses import JSONResponse
        return JSONResponse({"ok": False, "reason": str(exc)}, status_code=422)
    notifier = await build_notifier(request, body.tenant or "")
    return await notifier.send_carousel(
        recipient=body.recipient, tenant=body.tenant or "", template_name=body.template_name,
        language=body.language, components=components, user_id=body.user_id, event=body.event or "",
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
    """The internal twin of POST /api/v1/notifications/operator/whatsapp/embedded-signup.

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
            outcome = await WhatsAppNumberIndex(request.app.state.container.datastore).bind(
                body.phone_number_id, body.tenant)
        except Exception:  # noqa: BLE001
            outcome = None
        if outcome == WhatsAppNumberIndex.CONFLICT:
            # The number already routes to ANOTHER tenant. We did not steal it (first-wins); record the
            # collision loudly so an operator resolves it — a silent overwrite here was the reported gap.
            await request.app.state.auditor.record(
                "whatsapp.number_bind_conflict", actor="", tenant=body.tenant,
                resource=f"phone:{body.phone_number_id}",
                ip=request.client.host if request.client else "")
            result["number_bind"] = "conflict"
    return result


@router.get("/whatsapp/platform-status", dependencies=[Depends(require_internal_token)])
async def whatsapp_platform_status(request: Request) -> dict:
    """Operator glance for a platform WhatsApp status panel — AGGREGATE COUNTS ONLY, never row dumps.

    Every figure is bounded (capped at NOTIFY_PLATFORM_STATUS_CAP) and marked `capped` past the cap,
    so a panel that polls this can never trigger an unbounded scan. Delivery outcomes come from a
    single indexed read over the most-recent WhatsApp deliveries; the bound-number and marketing
    opt-out figures are capped counts of their maps. No PII, no per-row data leaves this route."""
    from app.core.stores import DeliveryStore, PreferenceStore, WhatsAppNumberIndex
    s = get_settings()
    ds = request.app.state.container.datastore
    cap = int(s.NOTIFY_PLATFORM_STATUS_CAP)
    return {
        "deliveries": await DeliveryStore(ds).whatsapp_outcome_counts(cap),
        "bound_numbers": await WhatsAppNumberIndex(ds).count(cap),
        "marketing_opt_outs": await PreferenceStore(ds).marketing_opt_out_count(cap),
        "cap": cap,
    }


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


@router.get("/whatsapp/inbound", dependencies=[Depends(require_internal_token)])
async def inbound_messages(request: Request, after: str = "", limit: int = 200) -> dict:
    """The catch-up read for INBOUND WhatsApp messages — the same shape as /notifications/receipts.

    Inbound `whatsapp.message` events go on the bus best-effort (at-most-once), so a pod that restarts
    through one hears it never and a customer's reply is silently never answered. This is how a
    consumer reconciles that gap: read forward from a cursor, persist the returned `cursor` as its
    watermark, repeat. Each row carries the wamid, sender, tenant (resolved from the receiving number)
    and the raw Meta message; ordered by the message timestamp; a message that only lived on the bus
    is still here. Idempotent by wamid, so a re-delivered webhook is one row, never a duplicate."""
    from app.core.stores import InboundStore
    store = InboundStore(request.app.state.container.datastore)
    rows, cursor = await store.changed_since(after=after or None, limit=max(1, min(1000, limit)))
    return {"messages": rows, "cursor": cursor}


@router.get("/whatsapp/config", dependencies=[Depends(require_internal_token)])
async def whatsapp_config(request: Request) -> dict:
    """The non-secret Embedded-Signup client config a product console needs at runtime — the Meta App
    ID, the Facebook Login config id, and the Graph version. None of these is a secret: all three reach
    Meta's JS SDK in the owner's browser on every signup popup, so a console reading them live beats
    baking them into a JS bundle at build time. Platform-wide (not tenant-scoped) — the product side
    tenant-scopes and gates before the console sees it.

    Reads the runtime sealed override (secret store, platform scope) first, then the config default; the
    app SECRET is NEVER returned. `configured` is true only when both app_id and config_id are present."""
    s = get_settings()
    store = getattr(request.app.state, "secret_store", None)

    async def _val(name: str, fallback: str) -> str:
        if store is not None:
            try:
                return await store.get(tenant="", name=name, fallback=fallback)
            except Exception:  # noqa: BLE001 — a store hiccup falls back to the config value
                return fallback
        return fallback

    app_id = (await _val("META_APP_ID", s.META_APP_ID) or "").strip()
    config_id = (await _val("META_APP_CONFIG_ID", s.META_APP_CONFIG_ID) or "").strip()
    graph_version = s.WHATSAPP_GRAPH_BASE.rstrip("/").rsplit("/", 1)[-1]
    return {"app_id": app_id, "config_id": config_id, "graph_version": graph_version,
            "configured": bool(app_id and config_id)}


@router.get("/whatsapp/coexistence/echoes", dependencies=[Depends(require_internal_token)])
async def coexistence_echoes(request: Request, after: str = "", limit: int = 200) -> dict:
    """Catch-up read for COEXISTENCE message echoes — messages the shop owner sent from the phone's own
    WhatsApp Business app on the connected number. Same shape/contract as /whatsapp/inbound: read
    forward from a cursor, persist it as the watermark, repeat. Without these the conversation an
    assistant/reminders consumer reconstructs has a hole (the owner's side is invisible). Idempotent by
    wamid; ordered by timestamp; a message that only lived on the bus is still here."""
    from app.core.stores import CoexistenceStore
    store = CoexistenceStore(request.app.state.container.datastore)
    rows, cursor = await store.echoes_since(after=after or None, limit=max(1, min(1000, limit)))
    return {"messages": rows, "cursor": cursor}


@router.get("/whatsapp/coexistence/history", dependencies=[Depends(require_internal_token)])
async def coexistence_history(request: Request, after: str = "", limit: int = 200) -> dict:
    """Catch-up read for the COEXISTENCE history import — up to 180 days of past messages the customer
    chose to share, once. Same shape as the others; each row carries the thread's contact + the raw Meta
    message. Idempotent by wamid; ordered by timestamp — page through with the cursor to import it all."""
    from app.core.stores import CoexistenceStore
    store = CoexistenceStore(request.app.state.container.datastore)
    rows, cursor = await store.history_since(after=after or None, limit=max(1, min(1000, limit)))
    return {"messages": rows, "cursor": cursor}


@router.get("/whatsapp/coexistence/contacts", dependencies=[Depends(require_internal_token)])
async def coexistence_contacts(request: Request, after: str = "", limit: int = 200) -> dict:
    """Catch-up read for the COEXISTENCE contact sync — the business's contacts at onboarding and on
    every add/remove. One row per contact number per tenant (latest state wins); each carries the
    action (add|remove) and the contact fields. Ordered by timestamp; page with the cursor."""
    from app.core.stores import CoexistenceStore
    store = CoexistenceStore(request.app.state.container.datastore)
    rows, cursor = await store.contacts_since(after=after or None, limit=max(1, min(1000, limit)))
    return {"contacts": rows, "cursor": cursor}


@router.post("/whatsapp/coexistence/purge", dependencies=[Depends(require_internal_token)])
async def coexistence_purge(request: Request, limit: int = 1000) -> dict:
    """Reap coexistence rows past their retention window — the scheduler (job-service) calls this
    periodically so a shop's stored conversations never live forever. Bounded per call; re-invoke until
    `removed` is 0. Recorded so the retention actually running (or silently not) is provable."""
    from app.core.stores import CoexistenceStore
    s = get_settings()
    store = CoexistenceStore(
        request.app.state.container.datastore,
        retention_seconds=max(0, s.WHATSAPP_COEXISTENCE_RETENTION_DAYS) * 86400)
    removed = await store.purge_expired(limit=max(1, min(5000, limit)))
    await request.app.state.auditor.record(
        "whatsapp_coexistence.purged", actor="system", tenant="",
        resource="wa_coexistence", ip=request.client.host if request.client else "",
        metadata={"removed": str(removed), "retention_days": str(s.WHATSAPP_COEXISTENCE_RETENTION_DAYS)})
    return {"removed": removed}


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
    #
    # The erasure receipt is a PERMANENT, hash-chained row (Art 17 accountability: proof we deleted),
    # so it must NOT carry the raw subject — GDPR data minimisation for logs. We record the PSEUDONYM
    # (subject_ref, same salted ref the ack returns and `actor` already uses), which proves WHOSE data
    # was erased and answers a DPA, without turning the tamper-evident log into a PII store. Correlation
    # to the DSAR is by compliance-service's own request id, not by a raw number here.
    from app.core.pii import subject_ref
    ref = subject_ref(s.PII_SALT, body.tenant, body.subject)
    await request.app.state.auditor.record(
        "subject.erased", actor="", tenant=body.tenant, resource=f"subject_ref:{ref}",
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
    # Optional Flow BUTTON to declare on this template (approved by Meta at creation), so a later send
    # can carry a per-send flow action. {"flow_id"|"flow_name", "flow_action", "navigate_screen", "text"}.
    flow_button: dict = Field(default_factory=dict)
    # Optional CAROUSEL: {"cards": [{"header_format":"IMAGE"|"VIDEO", "header_example_handle", "body"?,
    # "body_examples"?, "buttons":[…]}]}. 2–10 cards, all the same shape. Validated by the registry.
    carousel: dict = Field(default_factory=dict)
    # Optional FOOTER: a short fixed grey line under the message. Text-only, 60 chars max, no
    # variables — validated by the registry against Meta's rule.
    footer: str = Field(default="", max_length=60)


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
                               examples=body.examples, flow_button=body.flow_button,
                               carousel=body.carousel, footer=body.footer)
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


class WaFlowProvisionIn(BaseModel):
    tenant: str = Field(min_length=1, max_length=128)
    name: str = Field(min_length=1, max_length=256)          # the flow_NAME templates reference
    categories: list[str] = Field(min_length=1)              # Meta's fixed set; ≥1
    # Exactly one source: author-from-json OR clone-from-master. The provisioner enforces the XOR.
    flow_json: str = Field(default="", max_length=200_000)   # stringified Flow JSON (create-from-scratch)
    clone_flow_id: str = Field(default="", max_length=128)   # a master Flow id to copy (per-shop clone)
    endpoint_uri: str = Field(default="", max_length=2_048)  # required by Meta for Flow JSON v3.0+
    publish: bool = True                                     # publish so templates can reference it live


@router.post("/whatsapp/flows/provision", dependencies=[Depends(require_internal_token)])
async def provision_whatsapp_flow_internal(request: Request, body: WaFlowProvisionIn) -> dict:
    """Ensure a PUBLISHED WhatsApp Flow named `name` exists on this tenant's WABA — create/clone +
    publish it if absent, or find it if already there. IDEMPOTENT BY NAME.

    WHY IT EXISTS: a Flow is WABA-scoped (Meta's multi-WABA path is a name-matched CLONE with a new
    id), so in a model where every tenant connects its own WABA, each tenant needs its own Flow —
    provisioned at connect time, right where the template library is registered. This is the callable
    primitive for that step: the adopter's connect job calls it (create-from-scratch with `flow_json`,
    or the recommended clone-from-master with `clone_flow_id`), then registers templates whose
    flow_button references the Flow by flow_NAME — stable across every shop's clone, unlike the id.

    Idempotent because a PUBLISHED Flow is immutable and cannot be deleted: a re-run finds the existing
    Flow by name and returns it (publishing it only if it is still a draft), never a second copy."""
    import asyncio

    from app.api.v1.notifications import build_flow_manager
    from app.core.whatsapp_flows import FlowError, ensure_flow

    manager = await build_flow_manager(request, body.tenant)
    try:
        # The whole ensure — list + create/clone + publish — is blocking Graph I/O, so it runs off the
        # event loop. Validation errors (bad category, both/neither source) surface as a clean 422.
        result = await asyncio.to_thread(
            ensure_flow, manager=manager, name=body.name, categories=body.categories,
            flow_json=body.flow_json, clone_flow_id=body.clone_flow_id,
            endpoint_uri=body.endpoint_uri, publish=body.publish)
    except FlowError as exc:
        raise HTTPException(422, str(exc)) from exc
    await request.app.state.auditor.record(
        "whatsapp_flow.provisioned", actor="", tenant=body.tenant,
        resource=f"wa_flow:{body.name}", ip=request.client.host if request.client else "",
        metadata={"created": str(result.created), "published": str(result.published),
                  "status": result.status})
    return result.public()


class WaFlowEncryptionEnsureIn(BaseModel):
    tenant: str = Field(min_length=1, max_length=128)
    # Which of the tenant's numbers to register the key on. Optional — defaults to the tenant's resolved
    # primary number; a tenant with several numbers calls this once per number (the key is the same).
    phone_number_id: str = Field(default="", max_length=64)


@router.post("/whatsapp/flow-encryption/ensure", dependencies=[Depends(require_internal_token)])
async def ensure_flow_encryption_internal(request: Request, body: WaFlowEncryptionEnsureIn) -> dict:
    """Ensure this tenant's Flow data-endpoint encryption key is set up — the twin of the flow-provision
    call, run beside it at connect time. IDEMPOTENT: if a private key is already sealed for the tenant,
    it is NOT regenerated (that would rotate and break in-flight Flows); we only (re)register the SAME
    public key with Meta, which is itself idempotent (one active key per number, a new upload replaces).

    On a first run it generates an RSA keypair, seals the private half under the KEK (secret store), and
    registers the public half with Meta for the phone number — POST /{phone_number_id}/
    whatsapp_business_encryption — using the tenant's OWN sealed Graph token (the same conduit as
    flow/template provisioning; never our own identity). The private key never leaves the process. See
    app/core/flow_encryption.py and docs/design/whatsapp-booking-flow.md."""
    import asyncio

    from app.api.v1.notifications import build_flow_manager
    from app.core import flow_encryption as fe

    s = get_settings()
    store = getattr(request.app.state, "secret_store", None)
    if store is None:
        # No KEK ⇒ nothing to seal the private key with. Refuse rather than write it in the clear.
        raise HTTPException(503, "SECRETS_KEK is not configured — cannot seal the Flow private key")

    phone_id = body.phone_number_id
    if not phone_id:
        pool = getattr(request.app.state, "provider_pool", None)
        phone_id = (await pool.whatsapp_phone_number_id(body.tenant)) if pool is not None \
            else s.WHATSAPP_PHONE_NUMBER_ID
    if not phone_id:
        raise HTTPException(422, "no phone_number_id resolved for this tenant — pass one explicitly")

    name = s.WHATSAPP_FLOW_PRIVATE_KEY_SECRET
    info = await store.info(tenant=body.tenant, name=name)
    created = False
    if info is None:
        private_pem, public_pem = fe.generate_keypair(s.WHATSAPP_FLOW_KEY_SIZE)
        await store.set(tenant=body.tenant, name=name, value=private_pem,
                        actor_ref="system:flow-encryption")
        created = True
    else:
        # Already sealed — derive the public half and (re)register it; never regenerate (no rotation).
        private_pem = await store.get(tenant=body.tenant, name=name, fallback="")
        if not private_pem:
            raise HTTPException(500, "a sealed Flow key exists but could not be read")
        passphrase = None
        if s.WHATSAPP_FLOW_KEY_PASSPHRASE_SECRET:
            pp = await store.get(tenant=body.tenant, name=s.WHATSAPP_FLOW_KEY_PASSPHRASE_SECRET,
                                 fallback="")
            passphrase = pp.encode("utf-8") if pp else None
        public_pem = fe.public_key_from_private(private_pem, passphrase=passphrase)

    manager = await build_flow_manager(request, body.tenant)
    # Blocking Graph I/O off the event loop, like the provision route.
    up = await asyncio.to_thread(manager.set_encryption_public_key,
                                 phone_number_id=phone_id, public_key_pem=public_pem)
    status = ""
    if up.ok:
        try:
            status = await asyncio.to_thread(manager.encryption_public_key_status,
                                             phone_number_id=phone_id)
        except Exception:  # noqa: BLE001 — a read-back failure must not fail the ensure it just did
            status = ""
    result = fe.EncryptionKeyResult(
        ok=up.ok, created=created, uploaded=up.ok, phone_number_id=phone_id, status=status,
        reason="" if up.ok else (up.reason or "public key upload failed"))
    await request.app.state.auditor.record(
        "whatsapp_flow.encryption_ensured", actor="system", tenant=body.tenant,
        resource=f"wa_flow_encryption:{phone_id}", ip=request.client.host if request.client else "",
        metadata={"created": str(created), "uploaded": str(up.ok), "status": status})
    return result.public()


_MEDIA_TYPES = {"image/jpeg", "image/jpg", "image/png", "video/mp4"}


class WaMediaUploadIn(BaseModel):
    tenant: str = Field(min_length=1, max_length=128)
    file_type: str = Field(min_length=1, max_length=64)     # MIME: image/jpeg|image/png|video/mp4
    file_name: str = Field(default="upload.bin", max_length=256)
    # The media bytes, base64-encoded. JSON (not multipart) keeps the contract consistent with every
    # other internal route and needs no extra dependency; a connect-time upload of a header image is
    # small enough that the ~33% base64 overhead is a non-issue.
    data_base64: str = Field(min_length=1)


@router.post("/whatsapp/media/upload-handle", dependencies=[Depends(require_internal_token)])
async def upload_whatsapp_media_handle_internal(
        request: Request, body: WaMediaUploadIn,
        settings: Settings = Depends(get_settings)) -> dict:
    """Upload a media file via Meta's Resumable Upload API and return the reusable HANDLE.

    WHY IT EXISTS: a carousel template must be created with an example media handle per card, and Meta
    only accepts a handle its upload API produced — not a URL, not raw bytes. So the platform owns the
    upload→handle step (the adopter should never hunt for a handle by hand): upload the card's image/
    video here, drop the returned handle into that card's header_example_handle, then register the
    carousel template. App-scoped upload, authenticated with the tenant's own token (resolved in core).
    """
    import base64
    import binascii

    from app.api.v1.notifications import build_media_uploader

    ctype = body.file_type.lower()
    if ctype not in _MEDIA_TYPES:
        raise HTTPException(422, f"file_type must be one of {sorted(_MEDIA_TYPES)}, got {ctype!r}")
    try:
        data = base64.b64decode(body.data_base64, validate=True)
    except (ValueError, binascii.Error):  # malformed base64 is a caller error, not a 500
        raise HTTPException(422, "data_base64 is not valid base64")
    if not data:
        raise HTTPException(422, "the uploaded file is empty")
    if len(data) > settings.WHATSAPP_MEDIA_MAX_BYTES:
        raise HTTPException(413, f"file exceeds {settings.WHATSAPP_MEDIA_MAX_BYTES} bytes")
    uploader = await build_media_uploader(request, body.tenant)
    # The two-step upload is blocking Graph I/O; keep the loop free while Meta receives the bytes.
    res = await asyncio.to_thread(uploader.upload_handle, file_bytes=data, file_type=ctype,
                                  file_name=body.file_name or "upload.bin")
    if not res.ok:
        # A Meta-side failure is a clean {ok:false, reason}, not a 5xx — the caller can retry the upload.
        from starlette.responses import JSONResponse
        return JSONResponse({"ok": False, "reason": res.reason}, status_code=502)
    await request.app.state.auditor.record(
        "whatsapp_media.uploaded", actor="", tenant=body.tenant, resource="wa_media",
        ip=request.client.host if request.client else "",
        metadata={"file_type": ctype, "bytes": str(len(data)), "kind": "handle"})
    return {"ok": True, "handle": res.handle}


@router.post("/whatsapp/media/upload-id", dependencies=[Depends(require_internal_token)])
async def upload_whatsapp_media_id_internal(
        request: Request, body: WaMediaUploadIn,
        settings: Settings = Depends(get_settings)) -> dict:
    """Upload media to /{phone_number_id}/media and return the MEDIA ID used at SEND time.

    The send-time twin of /upload-handle: a carousel is CREATED with a resumable-upload handle but
    SENT with a phone-number-scoped media id (image.id / video.id per card). Same file, two Meta
    endpoints — so the platform owns both, and a caller never touches Meta's upload APIs directly.
    """
    import base64
    import binascii

    from app.api.v1.notifications import build_media_uploader

    ctype = body.file_type.lower()
    if ctype not in _MEDIA_TYPES:
        raise HTTPException(422, f"file_type must be one of {sorted(_MEDIA_TYPES)}, got {ctype!r}")
    try:
        data = base64.b64decode(body.data_base64, validate=True)
    except (ValueError, binascii.Error):
        raise HTTPException(422, "data_base64 is not valid base64")
    if not data:
        raise HTTPException(422, "the uploaded file is empty")
    if len(data) > settings.WHATSAPP_MEDIA_MAX_BYTES:
        raise HTTPException(413, f"file exceeds {settings.WHATSAPP_MEDIA_MAX_BYTES} bytes")
    pool = getattr(request.app.state, "provider_pool", None)
    phone_id = (await pool.whatsapp_phone_number_id(body.tenant)) if pool is not None \
        else settings.WHATSAPP_PHONE_NUMBER_ID
    uploader = await build_media_uploader(request, body.tenant)
    res = await asyncio.to_thread(uploader.upload_media_id, file_bytes=data, file_type=ctype,
                                  phone_number_id=phone_id, file_name=body.file_name or "upload.bin")
    if not res.ok:
        from starlette.responses import JSONResponse
        return JSONResponse({"ok": False, "reason": res.reason}, status_code=502)
    await request.app.state.auditor.record(
        "whatsapp_media.uploaded", actor="", tenant=body.tenant, resource="wa_media",
        ip=request.client.host if request.client else "",
        metadata={"file_type": ctype, "bytes": str(len(data)), "kind": "media_id"})
    return {"ok": True, "media_id": res.media_id}


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
            _bind_outcome = await WhatsAppNumberIndex(
                request.app.state.container.datastore).bind(phone_id, body.tenant)
        except Exception:  # noqa: BLE001 — best-effort, the number is registered regardless
            _bind_outcome = None
        if _bind_outcome == WhatsAppNumberIndex.CONFLICT:
            # First-wins: the number already routes to another tenant; we did NOT overwrite it. Record
            # the collision so it is never silent (the reported gap), then let registration report it.
            await request.app.state.auditor.record(
                "whatsapp.number_bind_conflict", actor="", tenant=body.tenant,
                resource=f"phone:{phone_id}", ip=request.client.host if request.client else "")
    await request.app.state.auditor.record(
        "whatsapp.number_registered", actor="", tenant=body.tenant,
        resource=f"phone:{phone_id}", ip=request.client.host if request.client else "",
        metadata={"registered": str(res.registered), "reason": res.reason})
    if res.registered:
        return {"registered": True}
    return JSONResponse({"registered": False, "reason": res.reason}, status_code=400)


class TwoStepPinIn(BaseModel):
    tenant: str = Field(min_length=1, max_length=128)
    phone_number_id: str | None = Field(default=None, max_length=128)   # PER NUMBER; falls back to the tenant's
    # Meta's 6-digit PIN, validated HERE so a malformed value never reaches Meta. Write-only.
    pin: str = Field(pattern=r"^[0-9]{6}$")


@router.post("/whatsapp/two-step-pin", dependencies=[Depends(require_internal_token)])
async def set_whatsapp_two_step_pin(request: Request, body: TwoStepPinIn):
    """Set / rotate a number's 6-digit two-step-verification PIN (Meta: POST /{phone_number_id} {pin}).

    DISTINCT from /whatsapp/register (which ACTIVATES sending using the PIN) — this sets the PIN
    itself. WRITE-ONLY: the PIN is never returned by any read, and the new value is re-sealed for this
    number so a later re-registration needs no human (same discipline as secrets.update)."""
    import asyncio

    from starlette.responses import JSONResponse

    from app.core.whatsapp_admin import set_two_step_pin
    from app.core.whatsapp_register import pin_secret_name
    s = get_settings()
    pool = getattr(request.app.state, "provider_pool", None)
    if pool is None:
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, "PROVIDER_POOL_UNAVAILABLE")
    token = await pool.whatsapp_access_token(body.tenant)
    phone_id = body.phone_number_id or await pool.whatsapp_phone_number_id(body.tenant)
    if not token or not phone_id:
        return JSONResponse({"ok": False, "reason": "NOT_CONNECTED"}, status_code=409)
    changed, reason = await asyncio.to_thread(
        set_two_step_pin, access_token=token, phone_number_id=phone_id, pin=body.pin,
        graph_base=s.WHATSAPP_ONBOARDING_GRAPH_BASE)
    if changed:
        # Re-seal the new PIN (best-effort — Meta already accepted it; a store hiccup must not report
        # failure). Never returned by any read.
        try:
            from app.api.v1.operator import _secret_store
            await _secret_store(request).set(tenant=body.tenant, name=pin_secret_name(phone_id),
                                             value=body.pin, actor_ref="")
        except Exception:  # noqa: BLE001
            pass
    # Audit the ACT (never the PIN value): a 2FA-PIN rotation is a credential-changing operation.
    await request.app.state.auditor.record(
        "whatsapp.two_step_pin_set", actor="", tenant=body.tenant,
        resource=f"phone:{phone_id}", ip=request.client.host if request.client else "",
        metadata={"ok": str(changed), "reason": reason})
    if changed:
        return {"ok": True}
    return JSONResponse({"ok": False, "reason": reason}, status_code=400)


@router.get("/whatsapp/subscribed-fields", dependencies=[Depends(require_internal_token)])
async def whatsapp_subscribed_fields(request: Request, tenant: str):
    """Which apps (and their webhook fields) are subscribed to this tenant's WABA, read live from Meta
    (GET /{WABA_ID}/subscribed_apps). READ-ONLY: Meta configures subscribed fields at the APP level,
    not per WABA, so this exists to CONFIRM what Meta is actually sending — an operator who changed the
    event handling had no way to verify it before."""
    if not tenant:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "tenant is required")
    import asyncio

    from app.core.whatsapp_admin import fetch_subscribed_apps
    s = get_settings()
    pool = getattr(request.app.state, "provider_pool", None)
    if pool is None:
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, "PROVIDER_POOL_UNAVAILABLE")
    token = await pool.whatsapp_access_token(tenant)
    waba = await pool.whatsapp_business_account_id(tenant)
    if not token or not waba:
        raise HTTPException(status.HTTP_409_CONFLICT, "NOT_CONNECTED")
    res = await asyncio.to_thread(fetch_subscribed_apps, access_token=token, waba_id=waba,
                                  graph_base=s.WHATSAPP_GRAPH_BASE)
    if not res.get("ok"):
        raise HTTPException(status.HTTP_502_BAD_GATEWAY, res.get("reason", "META_ERROR"))
    return {"apps": res["apps"]}


@router.get("/whatsapp/business-verification", dependencies=[Depends(require_internal_token)])
async def whatsapp_business_verification(request: Request, tenant: str):
    """The WABA's business-verification status + overall status, read live from Meta
    (GET /{WABA_ID}?fields=business_verification_status,status). It gates template-review speed and
    throughput upgrades, so an operator needs it readable rather than assumed."""
    if not tenant:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "tenant is required")
    import asyncio

    from app.core.whatsapp_admin import fetch_business_verification
    s = get_settings()
    pool = getattr(request.app.state, "provider_pool", None)
    if pool is None:
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, "PROVIDER_POOL_UNAVAILABLE")
    token = await pool.whatsapp_access_token(tenant)
    waba = await pool.whatsapp_business_account_id(tenant)
    if not token or not waba:
        raise HTTPException(status.HTTP_409_CONFLICT, "NOT_CONNECTED")
    res = await asyncio.to_thread(fetch_business_verification, access_token=token, waba_id=waba,
                                  graph_base=s.WHATSAPP_GRAPH_BASE)
    if not res.get("ok"):
        raise HTTPException(status.HTTP_502_BAD_GATEWAY, res.get("reason", "META_ERROR"))
    return {"business_verification_status": res["business_verification_status"],
            "status": res["status"]}


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
    # AUTHORITATIVE undo: also delete rows present now but absent from the archive
    # (backup-r2-01). Set only by the undo path; never combined with into_tenant.
    prune: bool = False


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
            declaration=BACKUP, prune=body.prune,
            scan_cap=settings.BACKUP_MAX_ROWS_PER_COLLECTION)
    except _backup.BackupError as exc:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, str(exc)) from exc
