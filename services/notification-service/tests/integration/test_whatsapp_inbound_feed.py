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
                      TOKEN_VERIFIER_DRIVER="hs256", TOKEN_HS256_SECRET="h" * 40,
                      PII_SALT="s" * 32,
                      # the durable inbound feed is opt-in now (master privacy switch defaults off)
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


def test_erasure_deletes_the_subjects_inbound_messages(client):
    # A customer's inbound messages are their own data with no delivery-proof basis — a DSAR must
    # remove them. Before this, erase() left wa_inbound untouched and the DSAR closed green over it.
    asyncio.run(WhatsAppNumberIndex(client.app.state.container.datastore).bind("PNID1", "acme"))
    _post(client, _inbound_webhook("wamid.A", "PNID1", sender="966500000001"))
    _post(client, _inbound_webhook("wamid.B", "PNID1", sender="966500000002"))   # another customer
    assert len(client.get("/internal/whatsapp/inbound", headers=_tok()).json()["messages"]) == 2

    r = client.post("/internal/privacy/erase", headers=_tok(),
                    json={"tenant": "acme", "subject": "966500000001"})
    assert r.status_code == 200, r.text
    assert any("inbound:1" in e for e in r.json()["erased"])
    # only that subject's message is gone; the other customer's remains
    left = client.get("/internal/whatsapp/inbound", headers=_tok()).json()["messages"]
    assert [m["wamid"] for m in left] == ["wamid.B"]


def test_inbound_purge_route_runs_and_is_audited(client):
    # The scheduled reaper endpoint responds (retention default keeps fresh rows, so 0 removed here);
    # the purge logic itself is covered in tests/unit/test_inbound_retention.
    asyncio.run(WhatsAppNumberIndex(client.app.state.container.datastore).bind("PNID1", "acme"))
    _post(client, _inbound_webhook("wamid.A", "PNID1"))
    r = client.post("/internal/whatsapp/inbound/purge", headers=_tok())
    assert r.status_code == 200 and r.json()["removed"] == 0   # fresh, still in-window


def test_master_switch_off_stores_nothing_but_still_acks_and_fans_out():
    """Default (WHATSAPP_CONVERSATION_STORE_ENABLED off): an inbound webhook is received and acked,
    the bus still fans out for live consumers, but NOT ONE row is written to wa_inbound — privacy by
    default. The catch-up feed is opt-in."""
    import asyncio as _asyncio

    saved = dict(os.environ)
    os.environ.update(ENV="test", META_APP_SECRET=SECRET, INTERNAL_SERVICE_TOKEN=ITOKEN,
                      TOKEN_VERIFIER_DRIVER="hs256", TOKEN_HS256_SECRET="h" * 40, PII_SALT="s" * 32)
    os.environ.pop("WHATSAPP_CONVERSATION_STORE_ENABLED", None)   # default OFF
    get_settings.cache_clear()
    try:
        from app.main import create_app
        with TestClient(create_app()) as c:
            _asyncio.run(WhatsAppNumberIndex(c.app.state.container.datastore).bind("PNID1", "acme"))
            seen = []

            async def _h(ev):
                seen.append(ev)
            _asyncio.run(c.app.state.container.event_bus.subscribe("whatsapp.message", _h))

            r = _post(c, _inbound_webhook("wamid.X", "PNID1"))
            assert r.status_code == 200 and r.json()["received"] is True     # acked
            assert seen, "the bus must still fan out for a live consumer"    # published
            # but nothing stored — the feed is empty
            assert c.get("/internal/whatsapp/inbound", headers=_tok()).json()["messages"] == []
    finally:
        os.environ.clear(); os.environ.update(saved)
        get_settings.cache_clear()
