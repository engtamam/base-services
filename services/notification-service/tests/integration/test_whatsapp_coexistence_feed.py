"""WhatsApp Coexistence catch-up feeds: a signed webhook's message_echoes / history / state_sync are
durably recorded and readable forward from a cursor, tenant-resolved from the receiving number,
idempotent by the item's stable id — the same reconciliation contract as /whatsapp/inbound, so an
assistant/reminders consumer never sees a hole where the owner's own-app activity was.

Payload shapes verified against Meta's coexistence webhook docs 2026-09-08.
"""
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
                      # coexistence storage needs BOTH the master store switch and its own enable
                      WHATSAPP_CONVERSATION_STORE_ENABLED="true",
                      WHATSAPP_COEXISTENCE_ENABLED="true")
    get_settings.cache_clear()
    from app.main import create_app
    with TestClient(create_app()) as c:
        asyncio.run(WhatsAppNumberIndex(c.app.state.container.datastore).bind("PNID1", "acme"))
        yield c
    os.environ.clear(); os.environ.update(saved)
    get_settings.cache_clear()


def _sign(raw: bytes) -> str:
    return "sha256=" + hmac.new(SECRET.encode(), raw, hashlib.sha256).hexdigest()


def _tok():
    return {"X-Internal-Token": ITOKEN}


def _post(client, field, value):
    payload = {"object": "whatsapp_business_account", "entry": [{"id": "WABA1", "changes": [{
        "field": field, "value": {"messaging_product": "whatsapp",
                                  "metadata": {"display_phone_number": "+966",
                                               "phone_number_id": "PNID1"}, **value}}]}]}
    raw = json.dumps(payload).encode()
    return client.post("/webhooks/whatsapp", content=raw, headers={"X-Hub-Signature-256": _sign(raw)})


def test_feeds_require_the_internal_token(client):
    for path in ("echoes", "history", "contacts"):
        assert client.get(f"/internal/whatsapp/coexistence/{path}").status_code == 401


def test_message_echo_is_recorded_replayable_and_tenant_resolved(client):
    r = _post(client, "smb_message_echoes", {"message_echoes": [
        {"from": "966555", "to": "966500", "id": "echo.A", "timestamp": "1700000000",
         "type": "text", "text": {"body": "on my way"}}]})
    assert r.status_code == 200, r.text
    body = client.get("/internal/whatsapp/coexistence/echoes", headers=_tok()).json()
    assert len(body["messages"]) == 1
    m = body["messages"][0]
    assert m["wamid"] == "echo.A" and m["tenant"] == "acme"
    assert m["message"]["text"]["body"] == "on my way"


def test_echo_is_idempotent_by_wamid(client):
    _post(client, "smb_message_echoes", {"message_echoes": [
        {"id": "echo.A", "timestamp": "1700000000", "type": "text", "text": {"body": "hi"}}]})
    # Meta re-delivers (retry) — must NOT create a second row
    _post(client, "smb_message_echoes", {"message_echoes": [
        {"id": "echo.A", "timestamp": "1700000000", "type": "text", "text": {"body": "hi"}}]})
    body = client.get("/internal/whatsapp/coexistence/echoes", headers=_tok()).json()
    assert len(body["messages"]) == 1


def test_history_messages_are_recorded_per_message(client):
    r = _post(client, "history", {"history": [{
        "metadata": {"phase": 0, "chunk_order": 1, "progress": 50},
        "threads": [{"id": "966500", "messages": [
            {"from": "966500", "to": "966555", "id": "h.1", "timestamp": "1699990000",
             "type": "text", "text": {"body": "old q"}},
            {"from": "966555", "to": "966500", "id": "h.2", "timestamp": "1699990005",
             "type": "text", "text": {"body": "old a"}}]}]}]})
    assert r.status_code == 200, r.text
    body = client.get("/internal/whatsapp/coexistence/history", headers=_tok()).json()
    ids = sorted(m["wamid"] for m in body["messages"])
    assert ids == ["h.1", "h.2"]
    assert body["messages"][0]["contact"] == "966500"      # the thread's WhatsApp user


def test_contact_state_sync_is_recorded_and_latest_wins(client):
    _post(client, "smb_app_state_sync", {"state_sync": [
        {"type": "contact", "action": "add", "metadata": {"timestamp": "1700000000"},
         "contact": {"phone_number": "966500", "full_name": "Sara", "first_name": "Sara"}}]})
    # a later change to the SAME contact updates in place (a sync = latest state per number)
    _post(client, "smb_app_state_sync", {"state_sync": [
        {"type": "contact", "action": "remove", "metadata": {"timestamp": "1700000100"},
         "contact": {"phone_number": "966500", "full_name": "Sara", "first_name": "Sara"}}]})
    body = client.get("/internal/whatsapp/coexistence/contacts", headers=_tok()).json()
    assert len(body["contacts"]) == 1
    assert body["contacts"][0]["action"] == "remove" and body["contacts"][0]["tenant"] == "acme"


def test_disabled_by_default_stores_nothing():
    # Without WHATSAPP_COEXISTENCE_ENABLED the durable half is off — nothing is stored (opt-in only).
    saved = dict(os.environ)
    os.environ.update(ENV="test", META_APP_SECRET=SECRET, INTERNAL_SERVICE_TOKEN=ITOKEN,
                      TOKEN_VERIFIER_DRIVER="hs256", TOKEN_HS256_SECRET="h" * 40)
    os.environ.pop("WHATSAPP_COEXISTENCE_ENABLED", None)
    get_settings.cache_clear()
    from app.main import create_app
    with TestClient(create_app()) as c:
        _post(c, "smb_message_echoes", {"message_echoes": [
            {"id": "echo.X", "to": "966500", "timestamp": "1700000000", "type": "text",
             "text": {"body": "hi"}}]})
        body = c.get("/internal/whatsapp/coexistence/echoes", headers=_tok()).json()
        assert body["messages"] == []
    os.environ.clear(); os.environ.update(saved)
    get_settings.cache_clear()


def test_purge_removes_expired_rows_only(client):
    import asyncio

    from app.core.stores import CoexistenceStore
    ds = client.app.state.container.datastore
    # one already-expired echo, one fresh — stamp expires_at directly via the store
    old = CoexistenceStore(ds, clock=lambda: 1000.0, retention_seconds=100)   # expires at 1100
    new = CoexistenceStore(ds, clock=lambda: 9000.0, retention_seconds=100)   # expires at 9100
    asyncio.run(old.record_echo(message_id="old", timestamp=1.0, tenant="acme", contact="966500",
                                phone_number_id="PNID1", waba_id="W", message={"id": "old"}))
    asyncio.run(new.record_echo(message_id="new", timestamp=2.0, tenant="acme", contact="966500",
                                phone_number_id="PNID1", waba_id="W", message={"id": "new"}))
    # purge as of t=5000: only the expired one goes
    removed = asyncio.run(CoexistenceStore(ds).purge_expired(now=5000.0))
    assert removed == 1
    left = client.get("/internal/whatsapp/coexistence/echoes", headers=_tok()).json()
    assert [m["wamid"] for m in left["messages"]] == ["new"]


def test_erasure_deletes_a_subjects_coexistence_data(client):
    import asyncio

    from app.core.erasure import NotificationErasure
    _post(client, "smb_message_echoes", {"message_echoes": [
        {"id": "e1", "to": "966500", "timestamp": "1", "type": "text", "text": {"body": "hi"}}]})
    _post(client, "smb_app_state_sync", {"state_sync": [
        {"type": "contact", "action": "add", "metadata": {"timestamp": "1"},
         "contact": {"phone_number": "966500"}}]})
    eraser = NotificationErasure(store=client.app.state.container.datastore, pii_salt="pepper")
    ack = asyncio.run(eraser.erase("acme", "966500"))
    assert any(e.startswith("coexistence:") for e in ack["erased"])
    # the subject's rows are gone
    echoes = client.get("/internal/whatsapp/coexistence/echoes", headers=_tok()).json()
    contacts = client.get("/internal/whatsapp/coexistence/contacts", headers=_tok()).json()
    assert echoes["messages"] == [] and contacts["contacts"] == []
