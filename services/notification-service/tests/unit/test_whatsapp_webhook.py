"""The inbound Meta webhook — verify then fan out, and NEVER filter an unknown type.

The load-bearing behaviour: the GET handshake echoes the challenge only for the right verify token;
the POST is refused unless X-Hub-Signature-256 matches the app secret; and a verified body is fanned
out whole — receipts, account events, AND unrecognised future fields — because a receiver that drops
unknown types would recreate the exact silent failure it exists to fix.
"""
from __future__ import annotations

import asyncio
import hashlib
import hmac
import json

import pytest
from starlette.testclient import TestClient

from app.config import get_settings

SECRET = "app-secret-xyz"
VERIFY = "verify-nonce-123"


@pytest.fixture()
def client(monkeypatch):
    monkeypatch.setenv("META_APP_SECRET", SECRET)
    monkeypatch.setenv("WHATSAPP_WEBHOOK_VERIFY_TOKEN", VERIFY)
    # these tests exercise the us-path (inbound fans out); the default handover mode is now "meta"
    # (privacy-first), so opt this deployment into "us".
    monkeypatch.setenv("WHATSAPP_DEFAULT_HANDOVER_MODE", "us")
    get_settings.cache_clear()
    from app.main import create_app
    with TestClient(create_app()) as c:
        yield c
    get_settings.cache_clear()


def _sign(raw: bytes) -> str:
    return "sha256=" + hmac.new(SECRET.encode(), raw, hashlib.sha256).hexdigest()


# ── GET: the subscription handshake ───────────────────────────────────────────
def test_get_echoes_the_challenge_for_the_right_token(client):
    r = client.get("/webhooks/whatsapp", params={
        "hub.mode": "subscribe", "hub.verify_token": VERIFY, "hub.challenge": "42"})
    assert r.status_code == 200 and r.text == "42"


def test_get_refuses_a_wrong_or_missing_token(client):
    assert client.get("/webhooks/whatsapp", params={
        "hub.mode": "subscribe", "hub.verify_token": "wrong", "hub.challenge": "42"}).status_code == 403
    assert client.get("/webhooks/whatsapp").status_code == 403


# ── POST: signature, then fan-out ─────────────────────────────────────────────
def test_post_refuses_a_bad_or_missing_signature(client):
    body = json.dumps({"entry": []}).encode()
    assert client.post("/webhooks/whatsapp", content=body).status_code == 401           # no header
    assert client.post("/webhooks/whatsapp", content=body,
                       headers={"X-Hub-Signature-256": "sha256=deadbeef"}).status_code == 401


def _subscribe(client, *topics):
    seen: dict[str, list] = {t: [] for t in topics}
    bus = client.app.state.container.event_bus
    for t in topics:
        async def _h(ev, _t=t):
            seen[_t].append(ev)
        asyncio.run(bus.subscribe(t, _h))
    return seen


def test_post_fans_out_receipts_account_events_and_unknown_fields(client):
    seen = _subscribe(client, "whatsapp.messages", "whatsapp.status", "whatsapp.account_update",
                      "whatsapp.some_future_field")
    payload = {"object": "whatsapp_business_account", "entry": [{
        "id": "WABA1", "changes": [
            {"field": "messages", "value": {
                "metadata": {"phone_number_id": "PN1"},
                "statuses": [{"id": "wamid.1", "status": "read"}]}},
            {"field": "account_update", "value": {"event": "PARTNER_REMOVED"}},
            # an event type this service does not model yet — it must STILL be emitted, not dropped.
            {"field": "some_future_field", "value": {"anything": 1}},
        ]}]}
    raw = json.dumps(payload).encode()
    r = client.post("/webhooks/whatsapp", content=raw, headers={"X-Hub-Signature-256": _sign(raw)})
    assert r.status_code == 200 and r.json()["received"] is True

    # the whole `messages` change was emitted, AND its status was exploded into a first-class receipt
    assert seen["whatsapp.messages"] and seen["whatsapp.status"]
    assert seen["whatsapp.status"][0].payload["status"]["status"] == "read"
    assert seen["whatsapp.status"][0].payload["waba_id"] == "WABA1"
    # the account event reached its own topic
    assert seen["whatsapp.account_update"][0].payload["value"]["event"] == "PARTNER_REMOVED"
    # and the UNRECOGNISED field was fanned out, not filtered
    assert seen["whatsapp.some_future_field"], "an unknown event type must never be dropped"


def test_inbound_message_bus_event_carries_the_resolved_tenant(client):
    # The fast-path bus event must carry the tenant (resolved from the receiving number), not just the
    # durable catch-up record — otherwise a subscriber sees tenant="" and is forced onto the slow poll.
    from app.core.stores import WhatsAppNumberIndex
    asyncio.run(WhatsAppNumberIndex(client.app.state.container.datastore).bind("PNID9", "acme"))
    seen = _subscribe(client, "whatsapp.message")
    payload = {"object": "whatsapp_business_account", "entry": [{"id": "WABA1", "changes": [
        {"field": "messages", "value": {
            "metadata": {"phone_number_id": "PNID9"},
            "messages": [{"id": "wamid.abc", "from": "96650", "timestamp": "1700000000",
                          "type": "text", "text": {"body": "hi"}}]}}]}]}
    raw = json.dumps(payload).encode()
    assert client.post("/webhooks/whatsapp", content=raw,
                       headers={"X-Hub-Signature-256": _sign(raw)}).status_code == 200
    assert seen["whatsapp.message"], "the inbound message must fan out on the bus"
    ev = seen["whatsapp.message"][0]
    assert ev.payload["tenant"] == "acme"      # the fast path now carries attribution
    assert ev.payload["message"]["id"] == "wamid.abc"


def test_native_payment_status_fans_out_as_a_first_class_payment_event(client):
    # A native "Review and Pay" outcome arrives as a status of type "payment" (NOT a message). It must
    # still be emitted on the ordinary whatsapp.status axis AND exploded into whatsapp.payment so a
    # consumer sees it at once — but the payload is only a HINT (verified=False; confirm via lookup).
    seen = _subscribe(client, "whatsapp.status", "whatsapp.payment")
    payload = {"object": "whatsapp_business_account", "entry": [{"id": "WABA1", "changes": [
        {"field": "messages", "value": {
            "metadata": {"phone_number_id": "PN1"},
            "statuses": [{"id": "wamid.pay1", "status": "captured", "type": "payment",
                          "recipient_id": "96650",
                          "payment": {"reference_id": "ORD-9",
                                      "amount": {"value": 5000, "offset": 100}, "currency": "SAR",
                                      "transaction": {"id": "txn_1", "status": "captured"}}}]}}]}]}
    raw = json.dumps(payload).encode()
    assert client.post("/webhooks/whatsapp", content=raw,
                       headers={"X-Hub-Signature-256": _sign(raw)}).status_code == 200
    assert seen["whatsapp.status"], "the payment status must still ride the ordinary status axis"
    assert seen["whatsapp.payment"], "a type:payment status must fan out on whatsapp.payment"
    pay = seen["whatsapp.payment"][0].payload["payment"]
    assert pay["reference_id"] == "ORD-9" and pay["amount_minor"] == 5000 and pay["currency"] == "SAR"
    assert pay["status"] == "captured" and pay["transaction_id"] == "txn_1"
    assert pay["verified"] is False, "the webbook body is a hint; only a lookup marks it verified"


def test_a_plain_status_does_not_emit_a_payment_event(client):
    seen = _subscribe(client, "whatsapp.status", "whatsapp.payment")
    payload = {"object": "whatsapp_business_account", "entry": [{"id": "WABA1", "changes": [
        {"field": "messages", "value": {
            "metadata": {"phone_number_id": "PN1"},
            "statuses": [{"id": "wamid.2", "status": "delivered"}]}}]}]}
    raw = json.dumps(payload).encode()
    assert client.post("/webhooks/whatsapp", content=raw,
                       headers={"X-Hub-Signature-256": _sign(raw)}).status_code == 200
    assert seen["whatsapp.status"] and not seen["whatsapp.payment"]
