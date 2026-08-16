"""Matching a delivery receipt to the message it belongs to — the wamid join.

The wamid is the only key between a send and its receipt. These pin: it is captured on send and
returned; a status webhook resolves to the delivery, records the receipt (monotonic, on its own
axis), and fans out delivery_id; and the catch-up read replays receipts forward from a cursor so a
consumer that missed the at-most-once bus can reconcile.
"""
from __future__ import annotations

import asyncio
import hashlib
import hmac
import json

import pytest
from starlette.testclient import TestClient

from app.config import get_settings
from app.core.models import Delivery
from app.core.stores import DeliveryStore

SECRET = "app-secret-xyz"


def run(c):
    return asyncio.run(c)


# ── store: the join + the receipt axis ────────────────────────────────────────
def _store():
    from app.adapters.datastore.memory import MemoryDataStore
    return DeliveryStore(MemoryDataStore())


def test_a_delivery_is_found_by_its_wamid():
    st = _store()
    run(st.record(Delivery(id="d1", recipient="+966", channel="whatsapp", template_key="otp",
                           status="sent", provider_message_id="wamid.AAA")))
    assert run(st.by_provider_message_id("wamid.AAA")).id == "d1"
    assert run(st.by_provider_message_id("wamid.NOPE")) is None


def test_apply_receipt_records_on_a_separate_axis_and_is_monotonic():
    st = _store()
    run(st.record(Delivery(id="d1", recipient="+966", channel="whatsapp", template_key="otp",
                           status="sent", provider_message_id="w1")))
    assert run(st.apply_receipt("w1", "delivered", 100.0)) == "d1"
    assert run(st.apply_receipt("w1", "read", 200.0)) == "d1"
    d = run(st.by_provider_message_id("w1"))
    assert d.receipt_status == "read" and d.receipt_ts == 200.0
    assert d.status == "sent"                       # the send-state machine is untouched
    # an out-of-order older receipt does not regress the newer state
    run(st.apply_receipt("w1", "delivered", 150.0))
    assert run(st.by_provider_message_id("w1")).receipt_status == "read"


def test_apply_receipt_for_an_unknown_wamid_invents_nothing():
    assert run(_store().apply_receipt("ghost", "read", 1.0)) is None


def test_catch_up_read_replays_receipts_forward_by_cursor():
    st = _store()
    for i in range(3):
        run(st.record(Delivery(id=f"d{i}", recipient="+966", channel="whatsapp", template_key="t",
                               status="sent", provider_message_id=f"w{i}")))
        run(st.apply_receipt(f"w{i}", "delivered", 10.0 + i))
    first, cursor = run(st.changed_since(after=None, limit=2))
    assert [r["delivery_id"] for r in first] == ["d0", "d1"]        # oldest first
    rest, _ = run(st.changed_since(after=cursor, limit=2))
    assert [r["delivery_id"] for r in rest] == ["d2"]              # forward from the cursor, no gap


# ── the webhook resolves a status to its delivery ─────────────────────────────
@pytest.fixture()
def client(monkeypatch):
    monkeypatch.setenv("META_APP_SECRET", SECRET)
    monkeypatch.setenv("WHATSAPP_WEBHOOK_VERIFY_TOKEN", "v")
    get_settings.cache_clear()
    from app.main import create_app
    with TestClient(create_app()) as c:
        yield c
    get_settings.cache_clear()


def _sign(raw: bytes) -> str:
    return "sha256=" + hmac.new(SECRET.encode(), raw, hashlib.sha256).hexdigest()


def test_a_status_webhook_resolves_to_the_delivery_and_records_it(client):
    store = DeliveryStore(client.app.state.container.datastore)
    run(store.record(Delivery(id="d9", recipient="+966", channel="whatsapp", template_key="otp",
                              status="sent", provider_message_id="wamid.XYZ")))
    seen = []

    async def _h(ev):
        seen.append(ev)
    run(client.app.state.container.event_bus.subscribe("whatsapp.status", _h))

    payload = {"entry": [{"id": "WABA1", "changes": [
        {"field": "messages", "value": {"metadata": {"phone_number_id": "PN1"},
                                        "statuses": [{"id": "wamid.XYZ", "status": "read",
                                                      "timestamp": "1723000000"}]}}]}]}
    raw = json.dumps(payload).encode()
    r = client.post("/webhooks/whatsapp", content=raw, headers={"X-Hub-Signature-256": _sign(raw)})
    assert r.status_code == 200
    # the receipt was recorded on the delivery…
    assert run(store.by_provider_message_id("wamid.XYZ")).receipt_status == "read"
    # …and the fanned-out event carries the resolved delivery_id, so it is attributable
    assert seen and seen[0].payload["delivery_id"] == "d9"


def test_the_catch_up_route_is_token_authed(client):
    assert client.get("/internal/notifications/receipts").status_code == 401
