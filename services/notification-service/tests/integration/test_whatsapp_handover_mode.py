"""Per-number handover mode (us|meta) + the privacy resolution it drives. Default is `meta`
(privacy-first): a number is private unless opted into `us`. Precedence: global hard override >
per-number setting > deployment default. Fail-CLOSED to private on any resolver error — a lookup
hiccup must never leak a customer's content."""
from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import os

import pytest
from starlette.testclient import TestClient

from app.config import get_settings
from app.core.stores import HandoverModeStore, WhatsAppNumberIndex

SECRET = "app-secret-xyz"
ITOKEN = "i" * 40


def _app(**over) -> TestClient:
    env = {"ENV": "test", "META_APP_SECRET": SECRET, "INTERNAL_SERVICE_TOKEN": ITOKEN,
           "TOKEN_VERIFIER_DRIVER": "hs256", "TOKEN_HS256_SECRET": "h" * 40, "PII_SALT": "s" * 32,
           "WHATSAPP_CONVERSATION_STORE_ENABLED": "true"}
    env.update(over)
    os.environ.update(env)
    get_settings.cache_clear()
    from app.main import create_app
    return TestClient(create_app(), raise_server_exceptions=False)


@pytest.fixture()
def envguard():
    saved = dict(os.environ)
    yield
    os.environ.clear(); os.environ.update(saved)
    get_settings.cache_clear()


def _sign(raw: bytes) -> str:
    return "sha256=" + hmac.new(SECRET.encode(), raw, hashlib.sha256).hexdigest()


def _tok():
    return {"X-Internal-Token": ITOKEN}


def _inbound(pnid, wamid="w1", body="secret text"):
    return {"object": "whatsapp_business_account", "entry": [{"id": "WABA1", "changes": [{
        "field": "messages", "value": {"metadata": {"phone_number_id": pnid},
            "messages": [{"from": "96650", "id": wamid, "timestamp": "1700000000",
                          "type": "text", "text": {"body": body}}]}}]}]}


def _post(c, payload):
    raw = json.dumps(payload).encode()
    return c.post("/webhooks/whatsapp", content=raw, headers={"X-Hub-Signature-256": _sign(raw)})


def _sub(c, topic):
    seen = []

    async def _h(ev):
        seen.append(ev)
    asyncio.run(c.app.state.container.event_bus.subscribe(topic, _h))
    return seen


# -- the resolution matrix --------------------------------------------------------
def test_default_meta_drops_inbound(envguard):
    c = _app()   # default handover mode = meta
    seen = _sub(c, "whatsapp.message")
    assert _post(c, _inbound("PN1")).status_code == 200
    assert seen == []                                             # dropped by default
    assert c.get("/internal/whatsapp/inbound", headers=_tok()).json()["messages"] == []


def test_per_number_us_override_processed_while_default_meta_dropped(envguard):
    c = _app()
    asyncio.run(WhatsAppNumberIndex(c.app.state.container.datastore).bind("PN1", "acme"))
    assert c.post("/internal/whatsapp/handover-mode", headers=_tok(),
                  json={"phone_number_id": "PN1", "mode": "us"}).status_code == 200
    seen = _sub(c, "whatsapp.message")
    _post(c, _inbound("PN1", "wa"))       # us -> processed
    _post(c, _inbound("PN2", "wb"))       # meta default -> dropped
    assert [e.payload["message"]["id"] for e in seen] == ["wa"]


def test_global_hard_override_beats_a_per_number_us(envguard):
    c = _app(WHATSAPP_INBOUND_PRIVACY_MODE="true")
    c.post("/internal/whatsapp/handover-mode", headers=_tok(),
           json={"phone_number_id": "PN1", "mode": "us"})       # even an explicit us ...
    seen = _sub(c, "whatsapp.message")
    _post(c, _inbound("PN1"))
    assert seen == []                                            # ... the global override still drops it


def test_default_us_processes_but_a_meta_override_drops(envguard):
    c = _app(WHATSAPP_DEFAULT_HANDOVER_MODE="us")
    c.post("/internal/whatsapp/handover-mode", headers=_tok(),
           json={"phone_number_id": "PN2", "mode": "meta"})
    seen = _sub(c, "whatsapp.message")
    _post(c, _inbound("PN1", "wa"))       # default us -> processed
    _post(c, _inbound("PN2", "wb"))       # meta override -> dropped
    assert [e.payload["message"]["id"] for e in seen] == ["wa"]


# -- the edge case that matters most: fail-closed to private ---------------------
def test_a_resolver_error_fails_closed_to_private(envguard, monkeypatch):
    c = _app(WHATSAPP_DEFAULT_HANDOVER_MODE="us")   # default would PROCESS ...
    import app.core.stores as stores

    async def _boom(self, phone_number_id):
        raise RuntimeError("store down")
    monkeypatch.setattr(stores.HandoverModeStore, "get", _boom)

    seen = _sub(c, "whatsapp.message")
    r = _post(c, _inbound("PN1", body="must not leak"))
    assert r.status_code == 200                                  # still acked (no retry storm)
    assert seen == []                                           # ... but the error DROPS (private)
    assert "must not leak" not in json.dumps([e.payload for e in seen])


# -- mixed batch: two numbers, two modes, one request ----------------------------
def test_mixed_batch_isolates_per_number(envguard):
    c = _app(WHATSAPP_DEFAULT_HANDOVER_MODE="us")
    c.post("/internal/whatsapp/handover-mode", headers=_tok(),
           json={"phone_number_id": "PNmeta", "mode": "meta"})
    payload = {"object": "whatsapp_business_account", "entry": [{"id": "WABA1", "changes": [
        {"field": "messages", "value": {"metadata": {"phone_number_id": "PNus"},
            "messages": [{"from": "96650", "id": "u", "timestamp": "1", "type": "text",
                          "text": {"body": "ok to see"}}]}},
        {"field": "messages", "value": {"metadata": {"phone_number_id": "PNmeta"},
            "messages": [{"from": "96651", "id": "m", "timestamp": "2", "type": "text",
                          "text": {"body": "private"}}]}}]}]}
    seen = _sub(c, "whatsapp.message")
    assert _post(c, payload).status_code == 200
    assert [e.payload["message"]["id"] for e in seen] == ["u"]   # meta change dropped in same batch
    assert "private" not in json.dumps([e.payload for e in seen])


# -- operator surface ------------------------------------------------------------
def test_get_effective_mode_override_vs_default(envguard):
    c = _app()
    got = c.get("/internal/whatsapp/handover-mode?phone_number_id=PN1", headers=_tok()).json()
    assert got["mode"] == "meta" and got["source"] == "default" and got["private"] is True
    c.post("/internal/whatsapp/handover-mode", headers=_tok(),
           json={"phone_number_id": "PN1", "mode": "us"})
    got = c.get("/internal/whatsapp/handover-mode?phone_number_id=PN1", headers=_tok()).json()
    assert got["mode"] == "us" and got["source"] == "override" and got["private"] is False


def test_set_requires_token_and_valid_mode(envguard):
    c = _app()
    assert c.post("/internal/whatsapp/handover-mode",
                  json={"phone_number_id": "PN1", "mode": "us"}).status_code == 401
    assert c.post("/internal/whatsapp/handover-mode", headers=_tok(),
                  json={"phone_number_id": "PN1", "mode": "teleport"}).status_code == 422


def test_clear_reverts_to_default(envguard):
    c = _app()
    c.post("/internal/whatsapp/handover-mode", headers=_tok(),
           json={"phone_number_id": "PN1", "mode": "us"})
    assert c.post("/internal/whatsapp/handover-mode/clear?phone_number_id=PN1",
                  headers=_tok()).json()["cleared"] is True
    got = c.get("/internal/whatsapp/handover-mode?phone_number_id=PN1", headers=_tok()).json()
    assert got["mode"] == "meta" and got["source"] == "default"


# -- store unit ------------------------------------------------------------------
def test_store_rejects_an_invalid_mode():
    from app.adapters.datastore.memory import MemoryDataStore
    s = HandoverModeStore(MemoryDataStore())
    with pytest.raises(ValueError):
        asyncio.run(s.set(phone_number_id="PN1", mode="bogus"))
    with pytest.raises(ValueError):
        asyncio.run(s.set(phone_number_id="", mode="us"))
    assert asyncio.run(s.get("PN1")) is None
