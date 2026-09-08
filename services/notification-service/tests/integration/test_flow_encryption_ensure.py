"""POST /internal/whatsapp/flow-encryption/ensure — the connect-time key setup, twin of flows/provision.

Token-authed, idempotent (a re-run does NOT regenerate the sealed key — that would rotate and break
in-flight Flows — it only re-registers the same public key), driven through the console driver (no Meta
round-trip in dev). The private key is sealed in the secret store and never returned.
"""
from __future__ import annotations

import pytest
from starlette.testclient import TestClient

from app.config import get_settings


def _app(monkeypatch):
    monkeypatch.setenv("META_APP_SECRET", "s")
    monkeypatch.setenv("SECRETS_KEK", "0" * 64)          # required to seal the private key
    monkeypatch.setenv("PII_SALT", "pepper")
    get_settings.cache_clear()
    from app.main import create_app
    return TestClient(create_app(), raise_server_exceptions=False)


def _tok():
    return {"X-Internal-Token": get_settings().INTERNAL_SERVICE_TOKEN}


def test_requires_the_internal_token(monkeypatch):
    c = _app(monkeypatch)
    r = c.post("/internal/whatsapp/flow-encryption/ensure",
               json={"tenant": "acme", "phone_number_id": "pn1"})
    assert r.status_code == 401
    get_settings.cache_clear()


def test_first_run_creates_seals_and_uploads(monkeypatch):
    c = _app(monkeypatch)
    r = c.post("/internal/whatsapp/flow-encryption/ensure", headers=_tok(),
               json={"tenant": "acme", "phone_number_id": "pn1"})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["ok"] and body["created"] and body["uploaded"]
    assert body["phone_number_id"] == "pn1" and body["status"] == "VALID"
    # the private key was actually sealed for the tenant (present, but never returned in the body)
    import asyncio
    info = asyncio.new_event_loop().run_until_complete(
        c.app.state.secret_store.info(tenant="acme", name="WHATSAPP_FLOW_PRIVATE_KEY"))
    assert info is not None and "key" not in body and "private" not in str(body).lower()
    get_settings.cache_clear()


def test_is_idempotent_and_does_not_regenerate(monkeypatch):
    c = _app(monkeypatch)
    payload = {"tenant": "acme", "phone_number_id": "pn1"}
    first = c.post("/internal/whatsapp/flow-encryption/ensure", headers=_tok(), json=payload).json()

    import asyncio
    loop = asyncio.new_event_loop()
    sealed_before = loop.run_until_complete(
        c.app.state.secret_store.get(tenant="acme", name="WHATSAPP_FLOW_PRIVATE_KEY"))

    second = c.post("/internal/whatsapp/flow-encryption/ensure", headers=_tok(), json=payload).json()
    sealed_after = loop.run_until_complete(
        c.app.state.secret_store.get(tenant="acme", name="WHATSAPP_FLOW_PRIVATE_KEY"))

    assert first["created"] is True
    assert second["created"] is False           # re-run did NOT generate a new key
    assert second["uploaded"] is True           # but did re-register the same public key
    assert sealed_before == sealed_after         # the sealed private key is untouched (no rotation)
    get_settings.cache_clear()


def test_no_phone_number_is_422(monkeypatch):
    c = _app(monkeypatch)
    r = c.post("/internal/whatsapp/flow-encryption/ensure", headers=_tok(), json={"tenant": "acme"})
    # dev has no resolvable number and none was passed → a clear 422, not a silent success
    assert r.status_code == 422
    get_settings.cache_clear()
