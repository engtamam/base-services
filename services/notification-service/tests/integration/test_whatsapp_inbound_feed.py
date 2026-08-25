"""The durable inbound-message feed: a signed webhook's inbound messages are recorded and readable
forward from a cursor (GET /internal/whatsapp/inbound), tenant-resolved from the receiving number,
idempotent by wamid — so a missed bus event is recoverable and a re-delivered webhook is not a dup."""
from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import os

import pytest
from starlette.testclient import TestClient

from app.config import get_settings
from app.core.stores import WhatsAppNumberIndex

SECRET = "app-secret-xyz"
ITOKEN = "i" * 40


@pytest.fixture()
def client():
    saved = dict(os.environ)
    os.environ.update(ENV="test", META_APP_SECRET=SECRET, INTERNAL_SERVICE_TOKEN=ITOKEN,
                      TOKEN_VERIFIER_DRIVER="hs256", TOKEN_HS256_SECRET="h" * 40)
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


def _inbound_webhook(wamid: str, pnid: str, ts: str = "1700000000", sender: str = "966500000001"):
    return {"object": "whatsapp_business_account", "entry": [{"id": "WABA1", "changes": [{
        "field": "messages", "value": {
            "messaging_product": "whatsapp",
            "metadata": {"display_phone_number": "+966", "phone_number_id": pnid},
            "messages": [{"from": sender, "id": wamid, "timestamp": ts, "type": "text",
                          "text": {"body": "confirm"}}]}}]}]}


def _post(client, payload):
    raw = json.dumps(payload).encode()
    return client.post("/webhooks/whatsapp", content=raw, headers={"X-Hub-Signature-256": _sign(raw)})


def test_inbound_feed_requires_the_internal_token(client):
    assert client.get("/internal/whatsapp/inbound").status_code == 401


def test_a_webhook_message_is_recorded_and_replayable_with_tenant(client):
    # bind the receiving number to a tenant so inbound is attributable
    asyncio.run(WhatsAppNumberIndex(client.app.state.container.datastore).bind("PNID1", "acme"))
    assert _post(client, _inbound_webhook("wamid.A", "PNID1")).status_code == 200

    body = client.get("/internal/whatsapp/inbound", headers=_tok()).json()
    assert len(body["messages"]) == 1
    m = body["messages"][0]
    assert m["wamid"] == "wamid.A" and m["tenant"] == "acme" and m["from"] == "966500000001"
    assert m["message"]["text"]["body"] == "confirm"
    # cursor is None here (one row, not a full page) — the same "caught up" signal the receipts feed
    # gives; real forward-from-cursor pagination is exercised in the next test.


def test_cursor_advances_and_dedupes_by_wamid(client):
    asyncio.run(WhatsAppNumberIndex(client.app.state.container.datastore).bind("PNID1", "acme"))
    _post(client, _inbound_webhook("wamid.A", "PNID1", ts="1700000000"))
    _post(client, _inbound_webhook("wamid.B", "PNID1", ts="1700000005"))
    # Meta re-delivers wamid.A (retry) — must NOT create a second row
    _post(client, _inbound_webhook("wamid.A", "PNID1", ts="1700000000"))

    first = client.get("/internal/whatsapp/inbound?limit=1", headers=_tok()).json()
    assert len(first["messages"]) == 1 and first["messages"][0]["wamid"] == "wamid.A"
    # read forward from the cursor — the next page is B, and A is not repeated
    nxt = client.get(f"/internal/whatsapp/inbound?after={first['cursor']}", headers=_tok()).json()
    kids = [m["wamid"] for m in nxt["messages"]]
    assert kids == ["wamid.B"], f"expected only B after the cursor, got {kids}"
