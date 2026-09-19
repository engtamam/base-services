"""Inbound Meta WhatsApp webhook — the ONE receiver the platform owns, because it owns the app secret.

Meta posts delivery/read receipts, inbound messages, account-level events (account_update with
PARTNER_REMOVED / ACCOUNT_OFFBOARDED / PRIMARY_INACTIVITY), marketing opt-outs (user_preferences), and
the coexistence history sync here. A product service cannot receive them: verifying X-Hub-Signature-256
needs META_APP_SECRET, which never leaves this service. Without this receiver a dead connection stays
green forever, receipts never arrive, and opt-outs are ignored — all silently.

TWO RULES that keep this from reproducing the very "green checks on top of nothing" it exists to fix:

  1. VERIFY, FAIL CLOSED. GET is the subscription handshake — echo hub.challenge only when
     hub.verify_token matches our shared token (constant-time). POST is accepted only when its
     X-Hub-Signature-256 is the HMAC-SHA256 of the RAW body under the app secret (constant-time). No
     secret configured => cannot verify => refuse. An unverified event is not processed.

  2. NEVER FILTER. Every change is fanned out to the bus under its own Meta field name — a delivery
     status, an account_update, an unrecognised future field, all of them. Dropping an unknown type
     would silently recreate today's situation; we would rather emit an event nobody consumes yet.
"""
from __future__ import annotations

import hashlib
import hmac
import json as _json

from fastapi import APIRouter, HTTPException, Query, Request, status
from starlette.responses import PlainTextResponse

from app.config import get_settings
from app.core import whatsapp_optout as _optout
from app.core.whatsapp_interactive import parse_payment_status as _parse_payment_status
from app.ports.event_bus import Event

router = APIRouter(tags=["webhooks"])

# Topic prefix for every fanned-out WhatsApp event. A consumer subscribes to "whatsapp.messages" for
# receipts, "whatsapp.account_update" for connect/disconnect, etc. — and to the exact field name for
# anything not yet modelled, because the field name IS the type.
_TOPIC = "whatsapp"


def _valid_signature(raw: bytes, header: str, app_secret: str) -> bool:
    """X-Hub-Signature-256 == 'sha256=' + HMAC-SHA256(app_secret, raw_body). Constant-time; fail
    closed when either the secret or the header is absent — an unverifiable POST is refused."""
    if not app_secret or not header:
        return False
    expected = "sha256=" + hmac.new(app_secret.encode("utf-8"), raw, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, header)


@router.get("/webhooks/whatsapp")
async def verify_subscription(
        request: Request,
        hub_mode: str = Query(default="", alias="hub.mode"),
        hub_verify_token: str = Query(default="", alias="hub.verify_token"),
        hub_challenge: str = Query(default="", alias="hub.challenge")):
    """Meta's subscription handshake: echo hub.challenge iff the verify token matches (constant-time).

    Returns the challenge as PLAIN TEXT — Meta compares the body verbatim, so a JSON wrapper fails the
    subscription silently."""
    expected = get_settings().WHATSAPP_WEBHOOK_VERIFY_TOKEN
    if hub_mode == "subscribe" and expected and hmac.compare_digest(hub_verify_token, expected):
        return PlainTextResponse(hub_challenge)
    raise HTTPException(status.HTTP_403_FORBIDDEN, "VERIFY_FAILED")


async def _tenant_of(number_index, phone_id: str) -> str:
    """The tenant that owns a receiving number, or "" — a lookup miss must never stop a fan-out."""
    if number_index is None or not phone_id:
        return ""
    try:
        return await number_index.tenant_of(phone_id) or ""
    except Exception:  # noqa: BLE001
        return ""


# WhatsApp webhook fields whose payload is CONVERSATION CONTENT (customer/owner messages, shared
# history, contact PII). In privacy mode (a number handed to Meta Business Agent) none of these may
# be stored OR put on the bus — we must know nothing. Delivery `statuses` (receipts about OUR sends),
# account/template events, and the user_preferences opt-out are NOT content and still flow.
_CONTENT_FIELDS = frozenset({"smb_message_echoes", "history", "smb_app_state_sync"})
# Sub-keys of a `messages` change value that carry content — dropped in privacy mode; `metadata` and
# `statuses` (receipts) are kept so delivery attribution still works.
_MESSAGES_CONTENT_KEYS = frozenset({"messages", "contacts"})


async def _fan_out(bus, deliveries, data: dict, inbound=None, number_index=None,
                   coexistence=None, prefs=None, privacy_mode: bool = False) -> int:
    """Publish every change to the bus, NOTHING filtered. Returns how many events were emitted.

    Meta's shape: {object, entry:[{id: <waba id>, changes:[{field, value}]}]}. Each change's `field`
    is the event type (messages, account_update, user_preferences, message_template_status_update,
    history, …). We emit one event per change under `whatsapp.<field>`, and additionally EXPLODE a
    `messages` change's delivery/read statuses and inbound messages into their own first-class events.

    Each status is RESOLVED to the delivery it belongs to (by the wamid both sides carry) and recorded
    on that delivery, so `whatsapp.status` carries `delivery_id` — the receipt is attributable — and a
    consumer can also reconcile durably via the catch-up read even when the bus (at-most-once) drops it.
    """
    emitted = 0
    for entry in data.get("entry", []) or []:
        waba_id = entry.get("id", "")
        for change in entry.get("changes", []) or []:
            field = str(change.get("field", "") or "unknown")
            value = change.get("value", {}) or {}
            phone_id = (value.get("metadata", {}) or {}).get("phone_number_id", "")
            base = {"waba_id": waba_id, "phone_number_id": phone_id, "field": field}
            # PRIVACY MODE (number handed to Meta): a content field is dropped WHOLE — not stored, not
            # published, we know nothing. A `messages` change is REDACTED to {metadata, statuses} so
            # delivery receipts still flow but the inbound messages + contact profiles never leave.
            if privacy_mode and field in _CONTENT_FIELDS:
                continue
            pub_value = value
            if privacy_mode and field == "messages":
                pub_value = {k: v for k, v in value.items() if k not in _MESSAGES_CONTENT_KEYS}
            # 1) the change itself, under its own field name — never filtered, even if unrecognised.
            await bus.publish(f"{_TOPIC}.{field}", Event(type=f"{_TOPIC}.{field}",
                                                         payload={**base, "value": pub_value}))
            emitted += 1
            # A marketing OPT-OUT is a compliance fact, not a hint: persist it DURABLY here (the bus
            # is at-most-once — a dropped event would resume marketing to someone who unsubscribed,
            # which suspends WhatsApp accounts). The send path then refuses marketing to this number.
            if field == "user_preferences" and prefs is not None:
                try:
                    await _optout.apply_user_preferences(
                        prefs, value, number_index=number_index)
                except Exception:  # noqa: BLE001 — a parse hiccup must not fail the whole webhook
                    pass
            # 2) first-class receipts, RESOLVED to a delivery, so the receipt is attributable.
            for st in value.get("statuses", []) or []:
                delivery_id = None
                if deliveries is not None:
                    try:
                        delivery_id = await deliveries.apply_receipt(
                            str(st.get("id", "")), str(st.get("status", "")),
                            float(st.get("timestamp", 0) or 0))
                    except Exception:  # noqa: BLE001 — a receipt we cannot record must still fan out
                        delivery_id = None
                await bus.publish(f"{_TOPIC}.status", Event(
                    type=f"{_TOPIC}.status", payload={**base, "delivery_id": delivery_id, "status": st}))
                emitted += 1
                # A native-payment status (order_details "Review and Pay") arrives here as a status of
                # type "payment", NOT a message. Emit it as a first-class `whatsapp.payment` event so a
                # consumer sees it immediately — but the payload is a HINT ONLY: the consumer MUST
                # confirm via the lookup (POST /internal/whatsapp/pay/lookup) before acting on money,
                # never trusting the webhook body. (Durable capture + a reconcile sweep are P6.)
                if str(st.get("type", "")) == "payment":
                    parsed = _parse_payment_status(st)
                    if parsed is not None:
                        await bus.publish(f"{_TOPIC}.payment", Event(
                            type=f"{_TOPIC}.payment", payload={**base, "payment": parsed}))
                        emitted += 1
            for msg in (value.get("messages", []) or []) if not privacy_mode else []:
                # Resolve the tenant ONCE from the receiving number and put it on BOTH the durable
                # record AND the live bus event. It used to live only on the record, so a fast-path
                # bus subscriber saw tenant="" and had to fall back to the slower catch-up poll just to
                # learn whose message it is — the fast path is exactly where the attribution should be.
                tenant = ""
                if number_index is not None and phone_id:
                    try:
                        tenant = await number_index.tenant_of(phone_id) or ""
                    except Exception:  # noqa: BLE001 — a lookup miss must not stop the fan-out
                        tenant = ""
                # Durably append BEFORE the bus publish, so a missed bus event is still recoverable via
                # the catch-up read. Best-effort and idempotent (keyed by wamid): a store hiccup must
                # still let the event fan out, and Meta's retry re-records the same row, not a duplicate.
                if inbound is not None:
                    try:
                        await inbound.record(
                            wamid=str(msg.get("id", "")), sender=str(msg.get("from", "")),
                            timestamp=float(msg.get("timestamp", 0) or 0),
                            msg_type=str(msg.get("type", "")), tenant=tenant,
                            phone_number_id=phone_id, waba_id=waba_id, message=msg)
                    except Exception:  # noqa: BLE001 — a message we cannot record must still fan out
                        pass
                await bus.publish(f"{_TOPIC}.message",
                                  Event(type=f"{_TOPIC}.message",
                                        payload={**base, "tenant": tenant, "message": msg}))
                emitted += 1
            # 3) COEXISTENCE — the owner's own-app activity on the same number. Each item is durably
            # recorded (idempotent by its stable id) for the catch-up read, because the consumers that
            # need it (assistant/reminders) reconcile via that log, not the at-most-once bus — exactly
            # the reasoning behind the inbound + receipt logs. The generic whatsapp.<field> event above
            # already puts the whole change on the bus for liveness; here we add the durable half.
            if coexistence is not None and field == "smb_message_echoes":
                # A message the owner sent from the phone's own WhatsApp Business app — invisible to us
                # otherwise, so "the conversation" would have a hole. Also emitted first-class so a live
                # consumer sees it the same way it sees an inbound message.
                co_tenant = await _tenant_of(number_index, phone_id)
                for echo in value.get("message_echoes", []) or []:
                    try:
                        await coexistence.record_echo(
                            message_id=str(echo.get("id", "")),
                            timestamp=float(echo.get("timestamp", 0) or 0), tenant=co_tenant,
                            contact=str(echo.get("to", "")),
                            phone_number_id=phone_id, waba_id=waba_id, message=echo)
                    except Exception:  # noqa: BLE001 — an echo we cannot record must still fan out
                        pass
                    await bus.publish(f"{_TOPIC}.message_echo", Event(
                        type=f"{_TOPIC}.message_echo",
                        payload={**base, "tenant": co_tenant, "message": echo}))
                    emitted += 1
            elif coexistence is not None and field == "history":
                # Up to 180 days of past messages, once. Recorded per message (idempotent by id) so the
                # import is replayable; the generic whatsapp.history bus event carries the live signal.
                co_tenant = await _tenant_of(number_index, phone_id)
                for chunk in value.get("history", []) or []:
                    for thread in chunk.get("threads", []) or []:
                        contact = str(thread.get("id", ""))
                        for hm in thread.get("messages", []) or []:
                            try:
                                await coexistence.record_history_message(
                                    message_id=str(hm.get("id", "")),
                                    timestamp=float(hm.get("timestamp", 0) or 0), contact=contact,
                                    tenant=co_tenant, phone_number_id=phone_id, waba_id=waba_id,
                                    message=hm)
                            except Exception:  # noqa: BLE001 — best-effort per message
                                pass
            elif coexistence is not None and field == "smb_app_state_sync":
                # The business's contacts, at onboarding and on every add/remove. Recorded per contact
                # (latest state per number, per tenant).
                co_tenant = await _tenant_of(number_index, phone_id)
                for item in value.get("state_sync", []) or []:
                    c = item.get("contact", {}) or {}
                    try:
                        await coexistence.record_contact(
                            phone_number=str(c.get("phone_number", "")),
                            timestamp=float((item.get("metadata", {}) or {}).get("timestamp", 0) or 0),
                            action=str(item.get("action", "")), tenant=co_tenant,
                            phone_number_id=phone_id, waba_id=waba_id, contact=c)
                    except Exception:  # noqa: BLE001 — best-effort per contact
                        pass
    return emitted


@router.post("/webhooks/whatsapp")
async def receive(request: Request) -> dict:
    """Verify the signature over the RAW body, then fan out. A bad or missing signature is refused;
    a verified body is always fanned out whole — unknown event types included."""
    raw = await request.body()
    if not _valid_signature(raw, request.headers.get("X-Hub-Signature-256", ""),
                            get_settings().META_APP_SECRET):
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "BAD_SIGNATURE")
    try:
        data = _json.loads(raw or b"{}")
    except ValueError:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "BAD_JSON")
    from app.core.stores import (CoexistenceStore, DeliveryStore, InboundStore,
                                 PreferenceStore, WhatsAppNumberIndex)
    s = get_settings()
    bus = request.app.state.container.event_bus
    ds = request.app.state.container.datastore
    deliveries = DeliveryStore(ds)
    # MASTER privacy switch (default OFF): store NO conversation content unless the platform opted in.
    # OFF ⇒ inbound and coexistence are None ⇒ nothing is written; the webhook is still acked and the
    # bus still fans out for live consumers (fan-out guards `inbound is not None`/`coexistence is not
    # None`). Coexistence additionally needs its own enable flag.
    # FULL-PRIVACY mode wins over everything: content is dropped at the root, so nothing is stored or
    # published regardless of the store switch.
    privacy = s.WHATSAPP_INBOUND_PRIVACY_MODE
    inbound = None
    coexistence = None
    if s.WHATSAPP_CONVERSATION_STORE_ENABLED and not privacy:
        inbound = InboundStore(
            ds, retention_seconds=max(0, s.WHATSAPP_INBOUND_RETENTION_DAYS) * 86400)
        if s.WHATSAPP_COEXISTENCE_ENABLED:
            coexistence = CoexistenceStore(
                ds, retention_seconds=max(0, s.WHATSAPP_COEXISTENCE_RETENTION_DAYS) * 86400)
    emitted = await _fan_out(
        bus, deliveries, data if isinstance(data, dict) else {},
        inbound=inbound, number_index=WhatsAppNumberIndex(ds),
        coexistence=coexistence, prefs=PreferenceStore(ds), privacy_mode=privacy)
    # Meta retries on any non-2xx and treats the retries as duplicates, so acknowledge fast once the
    # events are on the bus. The count is for our observability, not for Meta.
    return {"received": True, "events": emitted}


async def _default_flow_handler(payload: dict) -> dict:
    """Until a booking engine is wired (app.state.flow_handler), a non-ping request gets a clean,
    encrypted 'unavailable' snackbar rather than a broken screen. Never raises — the route already
    handled `ping` and decryption; the reply must still be encryptable."""
    return {"screen": payload.get("screen") or "SUCCESS",
            "data": {"error_message": "Booking is not available right now."}}


async def _handle_flow(request: Request, tenant: str, handler_name: str = "booking"):
    """WhatsApp Flow DATA ENDPOINT — the encrypted callback that drives a Flow's screens.

    Meta POSTs {encrypted_flow_data, encrypted_aes_key, initial_vector}, signed with the app secret.
    We verify the signature, resolve THIS SHOP's sealed private key (by tenant — the shop is in the
    URL because we must pick the key before we can decrypt, and the tenant is not readable until
    after), decrypt, dispatch (ping here, else the booking handler bound to this tenant), and return
    the AES-GCM reply as a raw base64 body.

    A decryption failure returns HTTP 421 — Meta's signal to refresh the public key — and never says
    which step failed. OFF until enabled + a key exists (503), so the path is inert by default. We
    never call Meta: the public key + Flow JSON are uploaded by the platform its own way. See
    docs/design/whatsapp-booking-flow.md."""
    from app.core import flow_crypto, flow_dispatch as fd

    s = get_settings()
    if not s.WHATSAPP_FLOW_ENDPOINT_ENABLED:
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, "FLOW_ENDPOINT_DISABLED")
    raw = await request.body()
    if not _valid_signature(raw, request.headers.get("X-Hub-Signature-256", ""), s.META_APP_SECRET):
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "BAD_SIGNATURE")
    try:
        body = _json.loads(raw or b"{}")
        efd, eak, iv = body["encrypted_flow_data"], body["encrypted_aes_key"], body["initial_vector"]
    except (ValueError, KeyError, TypeError):
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "BAD_REQUEST")

    store = getattr(request.app.state, "secret_store", None)
    # The sealed private key + passphrase are loaded in core (sealed_config.load_private_key), not
    # here: a credential's plaintext read must not sit in an HTTP handler (safety:no-secret-reads).
    # The key is sealed per shop under the same name, scoped by tenant ("" = single-brand default).
    from app.core import sealed_config as _sc
    private_pem, passphrase = await _sc.load_private_key(
        store, tenant=tenant, key_name=s.WHATSAPP_FLOW_PRIVATE_KEY_SECRET,
        passphrase_name=s.WHATSAPP_FLOW_KEY_PASSPHRASE_SECRET)
    if not private_pem:
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, "FLOW_KEY_UNCONFIGURED")

    # Resolve the dynamic-Flow handler from the registry (L3). The current routes serve the "booking"
    # flow; a future route can carry another handler name — the generic layers do not care.
    registry = getattr(request.app.state, "flow_registry", None)
    chosen = registry.get(handler_name) if registry is not None else None

    async def handler(payload: dict) -> dict:
        if chosen is None:
            return await _default_flow_handler(payload)
        return await chosen.handle(payload, tenant=tenant)   # tenant from the URL (opt-in)

    try:
        reply = await fd.process(
            encrypted_flow_data=efd, encrypted_aes_key=eak, initial_vector=iv,
            private_key_pem=private_pem.encode("utf-8") if isinstance(private_pem, str) else private_pem,
            handler=handler, passphrase=passphrase)
    except flow_crypto.FlowCryptoError:
        # Meta refreshes its cached public key on a 421 and retries — the recovery path for a rotated
        # or mismatched key. The body is deliberately empty (no hint which step failed).
        return PlainTextResponse("", status_code=421)
    return PlainTextResponse(reply, status_code=200)


@router.post("/webhooks/whatsapp/flow")
async def flow_endpoint(request: Request):
    """Single-brand Flow endpoint (tenant \"\"). A multi-shop platform uses the per-tenant path."""
    return await _handle_flow(request, "")


@router.post("/webhooks/whatsapp/flow/{tenant}")
async def flow_endpoint_tenant(request: Request, tenant: str):
    """Per-shop Flow endpoint (defaults to the booking handler). The shop sets THIS URL on its Flow,
    so we can pick its key before decrypting. The tenant is opaque here; it scopes the key, the
    definition and the session."""
    return await _handle_flow(request, tenant)


@router.post("/webhooks/whatsapp/flow/{tenant}/{handler}")
async def flow_endpoint_handler(request: Request, tenant: str, handler: str):
    """Per-shop, per-FLOW endpoint — the shop points each dynamic Flow (booking, reschedule, a survey,
    ...) at its own {handler} URL, so one deployment serves many dynamic Flows off the generic layer."""
    return await _handle_flow(request, tenant, handler)
