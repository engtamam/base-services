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


async def _fan_out(bus, deliveries, data: dict, inbound=None, number_index=None) -> int:
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
            # 1) the change itself, under its own field name — never filtered, even if unrecognised.
            await bus.publish(f"{_TOPIC}.{field}", Event(type=f"{_TOPIC}.{field}",
                                                         payload={**base, "value": value}))
            emitted += 1
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
            for msg in value.get("messages", []) or []:
                # Durably append BEFORE the bus publish, so a missed bus event is still recoverable via
                # the catch-up read. Best-effort and idempotent (keyed by wamid): a store hiccup must
                # still let the event fan out, and Meta's retry re-records the same row, not a duplicate.
                if inbound is not None:
                    try:
                        tenant = ""
                        if number_index is not None and phone_id:
                            tenant = await number_index.tenant_of(phone_id) or ""
                        await inbound.record(
                            wamid=str(msg.get("id", "")), sender=str(msg.get("from", "")),
                            timestamp=float(msg.get("timestamp", 0) or 0),
                            msg_type=str(msg.get("type", "")), tenant=tenant,
                            phone_number_id=phone_id, waba_id=waba_id, message=msg)
                    except Exception:  # noqa: BLE001 — a message we cannot record must still fan out
                        pass
                await bus.publish(f"{_TOPIC}.message",
                                  Event(type=f"{_TOPIC}.message", payload={**base, "message": msg}))
                emitted += 1
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
    from app.core.stores import DeliveryStore, InboundStore, WhatsAppNumberIndex
    bus = request.app.state.container.event_bus
    ds = request.app.state.container.datastore
    deliveries = DeliveryStore(ds)
    emitted = await _fan_out(bus, deliveries, data if isinstance(data, dict) else {},
                             inbound=InboundStore(ds), number_index=WhatsAppNumberIndex(ds))
    # Meta retries on any non-2xx and treats the retries as duplicates, so acknowledge fast once the
    # events are on the bus. The count is for our observability, not for Meta.
    return {"received": True, "events": emitted}
