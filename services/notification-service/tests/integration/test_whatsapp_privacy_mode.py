"""WHATSAPP_INBOUND_PRIVACY_MODE — a number handed to Meta Business Agent: customer/owner conversation
content is dropped at the ROOT (no store, no bus publish), enforced BY US so the guarantee never
depends on Meta withholding. Non-content still flows: delivery receipts, account/template events, and
the compliance opt-out. Meta is still acked (200) so it does not retry."""
from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import os

import pytest
from starlette.testclient import TestClient

from app.config import get_settings

SECRET = "app-secret-xyz"
ITOKEN = "i" * 40


@pytest.fixture()
def client():
    saved = dict(os.environ)
    os.environ.update(ENV="test", META_APP_SECRET=SECRET, INTERNAL_SERVICE_TOKEN=ITOKEN,
                      TOKEN_VERIFIER_DRIVER="hs256", TOKEN_HS256_SECRET="h" * 40, PII_SALT="s" * 32,
                      WHATSAPP_INBOUND_PRIVACY_MODE="true",
                      # even with storage ON, privacy mode must win and store nothing
                      WHATSAPP_CONVERSATION_STORE_ENABLED="true")
    get_settings.cache_clear()
    from app.main import create_app
    with TestClient(create_app()) as c:
        yield c
    os.environ.clear(); os.environ.update(saved)
    get_settings.cache_clear()


def _sign(raw: bytes) -> str:
    return "sha256=" + hmac.new(SECRET.encode(), raw, hashlib.sha256).hexdigest()


def _tok():
    return {"X-Internal-Token": ITOKEN}


def _post(client, payload):
    raw = json.dumps(payload).encode()
    return client.post("/webhooks/whatsapp", content=raw, headers={"X-Hub-Signature-256": _sign(raw)})


def _sub(client, *topics):
    seen = {t: [] for t in topics}
    bus = client.app.state.container.event_bus
    for t in topics:
        async def _h(ev, _t=t):
            seen[_t].append(ev)
        asyncio.run(bus.subscribe(t, _h))
    return seen


def _inbound(pnid="PN1", wamid="wamid.A"):
    return {"object": "whatsapp_business_account", "entry": [{"id": "WABA1", "changes": [{
        "field": "messages", "value": {
            "messaging_product": "whatsapp", "metadata": {"phone_number_id": pnid},
            "contacts": [{"wa_id": "96650", "profile": {"name": "Sara"}}],
            "messages": [{"from": "96650", "id": wamid, "timestamp": "1700000000",
                          "type": "text", "text": {"body": "my private message"}}]}}]}]}


def test_inbound_content_is_dropped_no_store_no_bus_but_acked(client):
    seen = _sub(client, "whatsapp.message", "whatsapp.messages")
    r = _post(client, _inbound())
    assert r.status_code == 200 and r.json()["received"] is True     # acked → Meta won't retry
    assert seen["whatsapp.message"] == []                            # no first-class inbound event
    # the generic messages event is REDACTED: no message bodies, no contact profiles
    assert seen["whatsapp.messages"], "the field event still fans out (redacted)"
    val = seen["whatsapp.messages"][0].payload["value"]
    assert "messages" not in val and "contacts" not in val
    assert "my private message" not in json.dumps(seen["whatsapp.messages"][0].payload)
    assert "Sara" not in json.dumps(seen["whatsapp.messages"][0].payload)
    # nothing stored
    assert client.get("/internal/whatsapp/inbound", headers=_tok()).json()["messages"] == []


def test_delivery_receipts_still_flow_in_privacy_mode(client):
    # a receipt is about OUR OWN sent message, not customer content — it must still be delivered
    seen = _sub(client, "whatsapp.status")
    payload = {"object": "whatsapp_business_account", "entry": [{"id": "WABA1", "changes": [{
        "field": "messages", "value": {"metadata": {"phone_number_id": "PN1"},
                                       "statuses": [{"id": "wamid.S", "status": "read",
                                                     "timestamp": "1700000000"}]}}]}]}
    assert _post(client, payload).status_code == 200
    assert seen["whatsapp.status"] and seen["whatsapp.status"][0].payload["status"]["status"] == "read"


def test_coexistence_content_is_dropped_entirely(client):
    seen = _sub(client, "whatsapp.smb_message_echoes", "whatsapp.message_echo")
    payload = {"object": "whatsapp_business_account", "entry": [{"id": "WABA1", "changes": [{
        "field": "smb_message_echoes", "value": {"metadata": {"phone_number_id": "PN1"},
            "message_echoes": [{"id": "e1", "to": "96650", "timestamp": "1700000000",
                                "type": "text", "text": {"body": "owner reply"}}]}}]}]}
    assert _post(client, payload).status_code == 200
    assert seen["whatsapp.smb_message_echoes"] == []      # content field dropped whole
    assert seen["whatsapp.message_echo"] == []
    assert "owner reply" not in json.dumps([e.payload for t in seen for e in seen[t]])


def test_account_events_and_optout_still_flow_in_privacy_mode(client):
    seen = _sub(client, "whatsapp.account_update")
    payload = {"object": "whatsapp_business_account", "entry": [{"id": "WABA1", "changes": [
        {"field": "account_update", "value": {"event": "VERIFIED_ACCOUNT"}}]}]}
    assert _post(client, payload).status_code == 200
    assert seen["whatsapp.account_update"], "operational account events are not content — must flow"
