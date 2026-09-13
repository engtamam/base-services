# =============================================================================
# notification-service — user + admin routes
# =============================================================================
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request, status
from pydantic import BaseModel, Field

from app.api.auth import Principal, require_user
from app.api.authz import require_capability
from app.api.deps import get_guard
from app.config import get_settings
from app.core.models import Preference, Template
from app.core.notifier import NotificationService
from app.core.retry import RetryPolicy
from app.core.whatsapp_templates import (TemplateError, WhatsAppTemplateRegistry,
                                         WhatsAppTemplateStore)
from app.core.stores import (
    DeliveryStore, InAppStore, PreferenceStore, TemplateStore,
)

notifications_router = APIRouter(prefix="/api/v1/notifications", tags=["notifications"])
templates_router = APIRouter(prefix="/api/v1/templates", tags=["templates"])
wa_templates_router = APIRouter(prefix="/api/v1/whatsapp/templates", tags=["whatsapp-templates"])
wa_flows_router = APIRouter(prefix="/api/v1/whatsapp/flows", tags=["whatsapp-flows"])


@wa_flows_router.get("/{name}/preview-json")
async def flow_preview_json(request: Request, name: str,
                           principal: Principal = Depends(require_user)) -> dict:
    """The companion Flow JSON for a registered slot-reservation Flow (booking/delivery/visit/…), for
    an OWNER to preview the multi-screen Flow visually in the console — the owner-facing twin of
    GET /internal/whatsapp/flow/{name}-json. Read-only, no special capability beyond being a signed-in
    shop user; the JSON is generated from the platform's config definition, identical for every tenant.

    404 (same as the internal route): no engine under that name, an engine that produces no Flow JSON
    (loyalty/order_status), or a booking engine with no definition configured yet. Booking-style engines
    (booking/delivery/visit) AND the reschedule engine are previewable."""
    from app.core.flow_json import FlowPreviewUnavailable, build_flow_preview_json
    registry = getattr(request.app.state, "flow_registry", None)
    handler = registry.get(name) if registry is not None else None
    try:
        return await build_flow_preview_json(handler)
    except FlowPreviewUnavailable as exc:
        raise HTTPException(status.HTTP_404_NOT_FOUND, str(exc)) from exc
_WA_CAP = require_capability("notifications.templates")
# The GLOBAL notification-template catalogue and the cross-tenant delivery log are PLATFORM surfaces
# (one shared row per template key; deliveries span every tenant). They must NOT share the per-tenant
# WhatsApp-template capability: a shop that can edit its own WhatsApp templates could otherwise
# overwrite the shared 'password_reset' email every tenant renders from, or read every tenant's
# delivery log. A distinct platform capability fences them.
_PLATFORM_CAP = require_capability("platform.notifications")


def _wa_registry(request: Request) -> WhatsAppTemplateRegistry:
    return WhatsAppTemplateRegistry(WhatsAppTemplateStore(request.app.state.container.datastore))


async def build_template_manager(request: Request, tenant: str = ""):
    """The template manager scoped to THIS tenant's WhatsApp Business Account.

    A template belongs to exactly one WABA, so a multi-tenant adopter whose customers message under
    their own name has one WABA per tenant, and approval is per tenant too. The per-tenant resolution
    — which reads the tenant's token and WABA id — lives in the provider pool (core), because reading
    credential plaintext is not something a route may do. The pool returns None when the tenant has
    no WABA of its own, and we fall back to the boot-built manager, so the single-brand case is
    unchanged.
    """
    c = request.app.state.container
    pool = getattr(request.app.state, "provider_pool", None)
    if pool is not None:
        mgr = await pool.template_manager(tenant)
        if mgr is not None:
            return mgr
    return c.whatsapp_template_manager


async def build_flow_manager(request: Request, tenant: str = ""):
    """The WhatsApp Flow manager scoped to THIS tenant's WABA, or the boot one for the single-brand
    case. Same resolution as build_template_manager — a Flow, like a template, is WABA-scoped and its
    token/WABA read must happen in core (the pool), never in a route."""
    c = request.app.state.container
    pool = getattr(request.app.state, "provider_pool", None)
    if pool is not None:
        mgr = await pool.flow_manager(tenant)
        if mgr is not None:
            return mgr
    return c.whatsapp_flow_manager


async def build_media_uploader(request: Request, tenant: str = ""):
    """The WhatsApp media uploader scoped to THIS tenant's token, or the boot one. Same resolution as
    the template/flow managers — the token read happens in core (the pool), never in a route."""
    c = request.app.state.container
    pool = getattr(request.app.state, "provider_pool", None)
    if pool is not None:
        up = await pool.media_uploader(tenant)
        if up is not None:
            return up
    return c.whatsapp_media_uploader


async def whatsapp_messaging_limit(request: Request, tenant: str = "") -> dict:
    """This tenant's WhatsApp messaging limit + quality rating, read live from Meta with a short
    cache. The phone-number id is resolved in the pool (core), so the credential read never happens
    in a route; the manager already carries the tenant's token. Reported unsupported for a driver
    that cannot reach Meta (console in prod-less dev returns unlimited itself), and never fabricated.
    """
    import asyncio
    import json as _json

    c = request.app.state.container
    s = get_settings()
    key = f"wa_limit:{tenant or '_'}"
    cache = getattr(c, "cache", None)
    if cache is not None:
        hit = await cache.get(key)
        if hit:
            try:
                return {**_json.loads(hit), "cached": True}
            except (ValueError, TypeError):
                pass

    pool = getattr(request.app.state, "provider_pool", None)
    phone_id = (await pool.whatsapp_phone_number_id(tenant)) if pool is not None \
        else s.WHATSAPP_PHONE_NUMBER_ID
    mgr = await build_template_manager(request, tenant)
    fetch = getattr(mgr, "messaging_limit", None)
    if fetch is None:
        return {"ok": False, "error": "unsupported",
                "driver": getattr(mgr, "driver", "")}
    # Off the event loop: this is a blocking Graph GET, and a planning read must not stall the loop.
    res = await asyncio.to_thread(fetch, phone_number_id=phone_id)
    out = res.to_dict()
    if res.ok and cache is not None:
        await cache.set(key, _json.dumps(out), s.WHATSAPP_LIMIT_CACHE_TTL_SECONDS)
    return {**out, "cached": False}


class WaTemplateIn(BaseModel):
    name: str = Field(min_length=1, max_length=512)
    language: str = Field(min_length=2, max_length=16)
    category: str = Field(min_length=1, max_length=32)
    body: str = Field(default="", max_length=4_000)
    variables: list[str] = Field(default_factory=list)   # ordered parameter names (send-time keys)
    examples: list[str] = Field(default_factory=list)    # ordered example values Meta needs at review
    # Optional Flow BUTTON to declare on this template (approved at creation) — see build docs.
    flow_button: dict = Field(default_factory=dict)
    # Optional CAROUSEL: {"cards":[…]} — 2–10 media cards, uniform shape. Validated by the registry.
    carousel: dict = Field(default_factory=dict)
    # Optional FOOTER: a short fixed grey line under the message. Text-only, 60 chars max, no
    # variables — validated by the registry against Meta's rule.
    footer: str = Field(default="", max_length=60)


@wa_templates_router.post("", status_code=201, dependencies=[Depends(_WA_CAP)])
async def register_wa_template(request: Request, body: WaTemplateIn,
                               principal: Principal = Depends(require_user)) -> dict:
    """Register a WhatsApp template at RUNTIME (the admin panel adds one — no redeploy). Starts
    pending; submit it to Meta for approval. Tenant comes from the token."""
    await request.app.state.auditor.record(
        "whatsapp_template.registered", actor=principal.subject, tenant=principal.tenant or "",
        resource=f"wa_template:{body.name}", ip=request.client.host if request.client else "",
        metadata={"category": body.category, "language": body.language})
    try:
        t = await _wa_registry(request).register(
            tenant=principal.tenant or "", name=body.name, language=body.language,
            category=body.category, body=body.body, variables=body.variables,
            examples=body.examples, flow_button=body.flow_button, carousel=body.carousel,
            footer=body.footer)
    except TemplateError as exc:
        raise HTTPException(422, str(exc))
    return t.public()


@wa_templates_router.post("/{name}/{language}/submit", dependencies=[Depends(_WA_CAP)])
async def submit_wa_template(request: Request, name: str, language: str,
                             principal: Principal = Depends(require_user)) -> dict:
    """Submit a registered template to Meta for review."""
    await request.app.state.auditor.record(
        "whatsapp_template.submitted", actor=principal.subject, tenant=principal.tenant or "",
        resource=f"wa_template:{name}", ip=request.client.host if request.client else "")
    try:
        t = await _wa_registry(request).submit(
            tenant=principal.tenant or "", name=name, language=language,
            manager=await build_template_manager(request, principal.tenant or ""))
    except TemplateError as exc:
        raise HTTPException(404, str(exc))
    return t.public()


@wa_templates_router.get("", dependencies=[Depends(_WA_CAP)])
async def list_wa_templates(request: Request, principal: Principal = Depends(require_user),
                            status: str = "", limit: int = 100) -> dict:
    rows, cursor = await _wa_registry(request).list(
        principal.tenant or "", status=status, limit=max(1, min(200, limit)))
    return {"templates": rows, "cursor": cursor}


class WaTestSendIn(BaseModel):
    to: str = Field(min_length=3, max_length=32)                 # recipient MSISDN
    phone_number_id: str = Field(min_length=1, max_length=64)    # which of the tenant's numbers to send FROM
    mode: str = Field(default="text")                            # "text" | "template"
    body: str = Field(default="", max_length=4_000)             # mode=text
    template_name: str = Field(default="", max_length=512)       # mode=template
    language: str = Field(default="en", max_length=16)
    variables: list[str] = Field(default_factory=list)


@wa_templates_router.post("/test-send", dependencies=[Depends(_WA_CAP)])
async def test_send_wa(request: Request, body: WaTestSendIn,
                       principal: Principal = Depends(require_user)) -> dict:
    """Admin DIAGNOSTIC: send exactly one WhatsApp message to confirm a live connection. Deliberately
    bypasses WHATSAPP_CHAIN + build_notifier (the chain stays `console` until a real activation, so it
    cannot answer "is my connection working") — it resolves the provider straight from the pool by the
    named sending number, the SAME entitlement check /register and /status use: a number that is not
    this tenant's is refused (409), never sent from. No queue, no TemplateStore, no dedupe. Audited
    with only the last 4 digits of `to`. mode=text needs `body`; mode=template needs `template_name`."""
    import asyncio

    from starlette.responses import JSONResponse

    if body.mode not in ("text", "template"):
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, "mode must be text or template")
    tenant = principal.tenant or ""
    to = body.to.strip()
    # Write-ahead audit (last-4 only): a privileged manual send is exactly the action a review asks
    # about, and it must be on the record before the message can leave.
    await request.app.state.auditor.record(
        "whatsapp.test_send", actor=principal.subject, tenant=tenant, resource="whatsapp",
        ip=request.client.host if request.client else "",
        metadata={"to_last4": to[-4:], "mode": body.mode})

    pool = getattr(request.app.state, "provider_pool", None)
    provider = (await pool.whatsapp_for_identity(tenant, body.phone_number_id)
                if pool is not None else None)
    if provider is None:
        # Not entitled to this number, or no credentials — refuse, never fall back to another sender.
        # JSONResponse (not raise) so the machine-readable reason survives — same 409 {"reason":...}
        # shape as the sibling /whatsapp/register, which the console already special-cases.
        return JSONResponse({"ok": False, "reason": "NOT_CONNECTED"}, status_code=status.HTTP_409_CONFLICT)

    # whatsapp_for_identity returns a FailoverWhatsApp, whose send/send_text return the
    # (ok, driver, message_id) TUPLE the notifier contract uses — NOT a bare wamid. Unpack it and
    # trust `ok`: a real Meta reject (e.g. an unapproved template) comes back ok=False, mid="", and
    # must surface as SEND_FAILED — never a 200 with a non-wamid id. (The provider send is a blocking
    # Graph POST — off the event loop, see the notifier's own send wrap.)
    if body.mode == "template":
        if not body.template_name:
            raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, "template_name required for mode=template")
        ok, _driver, mid = await asyncio.to_thread(
            provider.send, to, body.template_name, body.language, body.variables)
    else:
        if not get_settings().NOTIFY_TEST_SEND_ALLOW_FREE_TEXT:
            # Free-text diagnostic sends bypass consent and the approved-template catalogue — a
            # spam vector. OFF by default; an operator opts in with NOTIFY_TEST_SEND_ALLOW_FREE_TEXT.
            # Template mode (policy-compliant) remains available for a connection test.
            raise HTTPException(status.HTTP_403_FORBIDDEN, "FREE_TEXT_TEST_SEND_DISABLED")
        if not body.body:
            raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, "body required for mode=text")
        ok, _driver, mid = await asyncio.to_thread(provider.send_text, to, body.body)

    if not ok or not mid:
        return JSONResponse({"ok": False, "reason": "SEND_FAILED"}, status_code=status.HTTP_502_BAD_GATEWAY)
    return {"ok": True, "message_id": mid, "mode": body.mode}


async def build_notifier(request: Request, tenant: str = "") -> NotificationService:
    """Assemble the service for this request.

    Async because the provider chains are resolved from credentials an operator can change after
    launch, rather than captured once at boot. The pool caches per tenant, so this is a dict lookup
    on all but the first send in each refresh window — see `core/provider_pool.py`.
    """
    c = request.app.state.container
    s = get_settings()
    store = c.datastore
    pool = getattr(request.app.state, "provider_pool", None)
    if pool is not None:
        email, sms, whatsapp = await pool.get(tenant)
    else:
        # No pool wired: the boot-built chains from the environment. Behaviour identical to before
        # operator-managed credentials existed.
        email, sms, whatsapp = c.email, c.sms, c.whatsapp
    return NotificationService(
        templates=TemplateStore(store), prefs=PreferenceStore(store),
        deliveries=DeliveryStore(store),
        inapp=InAppStore(store, page_size=s.FEED_PAGE_SIZE, scan_limit=s.FEED_SCAN_LIMIT),
        email=email, sms=sms, whatsapp=whatsapp,
        wa_templates=WhatsAppTemplateRegistry(WhatsAppTemplateStore(store)), renderer=c.renderer,
        email_from=s.EMAIL_FROM, default_locale=s.DEFAULT_LOCALE,
        dedupe_ttl_seconds=s.NOTIFY_DEDUPE_TTL_SECONDS,
        dedupe_states=s.notify_dedupe_states,
        delivery_mode=s.NOTIFY_DELIVERY_MODE,
        consent_gate=c.consent_gate, consent_purposes=s.notify_consent_purposes,
        consent_unknown=s.NOTIFY_CONSENT_UNKNOWN,
        retry=RetryPolicy(
            max_attempts=s.NOTIFY_MAX_ATTEMPTS,
            base_ms=s.NOTIFY_BACKOFF_BASE_MS, max_ms=s.NOTIFY_BACKOFF_MAX_MS,
            retryable=s.notify_retryable, permanent=s.notify_permanent,
            unknown_is_retryable=s.NOTIFY_UNKNOWN_ERROR_RETRYABLE),
        auditor=getattr(request.app.state, "auditor", None),
    )


# ── Schemas ─────────────────────────────────────────────────────────────────
class PreferenceIn(BaseModel):
    suppressed: bool = False
    opt_out_channels: list[str] = Field(default_factory=list)
    opt_out_events: list[str] = Field(default_factory=list)


class TemplateIn(BaseModel):
    key: str = Field(min_length=1, max_length=128)
    locale: str = Field(default="en", max_length=16)
    subject: str = Field(default="", max_length=512)
    body: str = Field(min_length=1, max_length=20000)
    version: int = 1
    # transactional (default) | marketing. A marketing template must be mapped to a consent purpose;
    # the upsert route enforces it so it can never be saved unmapped (and then sent unchecked).
    category: str = Field(default="transactional", pattern="^(transactional|marketing)$")


# ── User: in-app feed ────────────────────────────────────────────────────────
@notifications_router.get("")
async def feed(request: Request, principal: Principal = Depends(require_user),
               guard=Depends(get_guard)) -> dict:
    items = await (await build_notifier(request, principal.tenant)).feed(principal.subject)
    async with guard.step("respond") as step:
        body = {"items": items}
        step.result = body
    return body


@notifications_router.post("/{item_id}/read")
async def mark_read(request: Request, item_id: str,
                    principal: Principal = Depends(require_user)) -> dict:
    ok = await (await build_notifier(request, principal.tenant)).mark_read(principal.subject, item_id)
    if not ok:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "NOT_FOUND")
    await request.app.state.auditor.record(
        "notification.read", actor=principal.subject, tenant=principal.tenant or "",
        resource=f"notification:{item_id}", ip=request.client.host if request.client else "")
    return {"status": "ok"}


# ── User: preferences ────────────────────────────────────────────────────────
@notifications_router.get("/preferences")
async def get_preferences(request: Request, principal: Principal = Depends(require_user)) -> dict:
    pref = await PreferenceStore(request.app.state.container.datastore).get(principal.subject)
    return pref.to_dict()


@notifications_router.put("/preferences")
async def set_preferences(request: Request, body: PreferenceIn,
                          principal: Principal = Depends(require_user)) -> dict:
    # Write-ahead: a critical action the log cannot take must not happen at all, so this
    # runs BEFORE the mutation. Recording afterwards would 503 a caller whose action had
    # already succeeded, and leave it unrecorded.
    await request.app.state.auditor.record(
        "preferences.updated", actor=principal.subject, tenant=principal.tenant, resource="preferences",
        ip=request.client.host if request.client else "")
    pref = Preference(user=principal.subject, suppressed=body.suppressed,
                      opt_out_channels=body.opt_out_channels, opt_out_events=body.opt_out_events)
    await PreferenceStore(request.app.state.container.datastore).set(pref)
    return pref.to_dict()


# ── Admin: template management (capability-gated) ────────────────────────────
@templates_router.post("", dependencies=[Depends(_PLATFORM_CAP)])
async def upsert_template(request: Request, body: TemplateIn) -> dict:
    from app.core.consent_guard import is_unmapped_marketing
    # A marketing template with no consent-purpose mapping would send unchecked — refuse to save it.
    if is_unmapped_marketing(body.key, body.category, get_settings().notify_consent_purposes):
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            f"MARKETING_TEMPLATE_UNMAPPED:{body.key} — add it to NOTIFY_CONSENT_PURPOSES so the "
            f"send path checks consent before sending it")
    tmpl = Template(key=body.key, locale=body.locale, subject=body.subject,
                    body=body.body, version=body.version, category=body.category)
    await TemplateStore(request.app.state.container.datastore).upsert(tmpl)
    return tmpl.to_dict()


@templates_router.get("", dependencies=[Depends(_PLATFORM_CAP)])
async def list_templates(request: Request) -> dict:
    rows = await TemplateStore(request.app.state.container.datastore).list()
    return {"templates": rows}


def _mask_recipient(recipient: str) -> str:
    """Enough to correlate a delivery, not enough to be a recipient list. An operator debugging why
    a message failed needs to tell rows apart, not to harvest addresses."""
    if not recipient:
        return ""
    if "@" in recipient:                       # email: first char + domain
        local, _, domain = recipient.partition("@")
        return f"{local[:1]}***@{domain}"
    return recipient[:3] + "***" + recipient[-2:] if len(recipient) > 5 else "***"


@notifications_router.get("/deliveries",
                          dependencies=[Depends(_PLATFORM_CAP)])
async def list_deliveries(request: Request,
                          principal: Principal = Depends(require_user)) -> dict:
    """Recent delivery attempts across the platform — status, channel, template, provider and the
    error when one failed: the operator's answer to 'did that notification send, and why not?'. The
    delivery log was recorded and listable but reachable from no route until now. Recipients are
    MASKED — enough to correlate, never a harvestable address — and the access is audited."""
    await request.app.state.auditor.record(
        "notification.deliveries_viewed", actor=principal.subject, tenant=principal.tenant,
        resource="delivery_log", ip=request.client.host if request.client else "")
    rows = await DeliveryStore(request.app.state.container.datastore).list(
        get_settings().NOTIFY_DELIVERY_LIST_LIMIT)
    for r in rows:
        r["recipient"] = _mask_recipient(r.get("recipient", ""))
    return {"deliveries": rows, "count": len(rows)}
