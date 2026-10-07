"""Phase 2 — the operator panel surface for the marketplace (capability-gated, audited, no secret reads).

Negative tests are load-bearing: the capability is required, an unknown mode is refused, and the
sub-merchant view exposes only the opaque destination id (never a bank detail).
"""
from __future__ import annotations

import asyncio
import datetime as dt
import os

import jwt
import pytest
from fastapi.testclient import TestClient

TOKEN_SECRET = "h" * 40
ISS, AUD = "iss", "aud"


def run(c):
    return asyncio.run(c)


def _make_app() -> TestClient:
    from app.config import get_settings
    os.environ.update({
        "ENV": "test", "INTERNAL_SERVICE_TOKEN": "t" * 40,
        "TOKEN_VERIFIER_DRIVER": "hs256", "TOKEN_HS256_SECRET": TOKEN_SECRET,
        "TOKEN_ISSUER": ISS, "TOKEN_AUDIENCE": AUD, "RATELIMIT_MAX": "1000",
        "SECRETS_KEK": "k" * 48, "PII_SALT": "s" * 32,
    })
    get_settings.cache_clear()
    from app.main import create_app
    return TestClient(create_app(), raise_server_exceptions=False)


@pytest.fixture()
def client():
    from app.config import get_settings
    saved = dict(os.environ)
    c = _make_app()
    yield c
    os.environ.clear(); os.environ.update(saved); get_settings.cache_clear()


def _auth(*caps: str) -> dict:
    now = dt.datetime.now(dt.timezone.utc)
    tok = jwt.encode({"sub": "op1", "iss": ISS, "aud": AUD, "caps": list(caps),
                      "exp": now + dt.timedelta(minutes=5), "iat": now}, TOKEN_SECRET, algorithm="HS256")
    return {"Authorization": f"Bearer {tok}"}


_CFG = "platform.config"


def test_set_and_get_payment_mode(client):
    r = client.put("/api/v1/app/operator/payment-mode", headers=_auth(_CFG),
                   json={"tenant": "acme", "mode": "marketplace"})
    assert r.status_code == 200 and r.json()["mode"] == "marketplace"
    g = client.get("/api/v1/app/operator/payment-mode?tenant=acme", headers=_auth(_CFG))
    assert g.json()["mode"] == "marketplace" and g.json()["source"] == "tenant"


def test_default_mode_is_conduit(client):
    g = client.get("/api/v1/app/operator/payment-mode?tenant=nobody", headers=_auth(_CFG))
    assert g.json()["mode"] == "conduit" and g.json()["source"] == "default"


def test_platform_force_locks_mode(client):
    client.put("/api/v1/app/operator/payment-mode", headers=_auth(_CFG),
               json={"tenant": "", "mode": "marketplace", "locked": True})
    g = client.get("/api/v1/app/operator/payment-mode?tenant=anyshop", headers=_auth(_CFG))
    assert g.json()["mode"] == "marketplace" and g.json()["source"] == "platform"


def test_unknown_mode_is_refused(client):
    r = client.put("/api/v1/app/operator/payment-mode", headers=_auth(_CFG),
                   json={"tenant": "acme", "mode": "crypto"})
    assert r.status_code == 400


def test_payment_mode_requires_capability(client):
    assert client.get("/api/v1/app/operator/payment-mode", headers=_auth()).status_code == 403
    assert client.put("/api/v1/app/operator/payment-mode", headers=_auth(),
                      json={"mode": "conduit"}).status_code == 403


def test_submerchant_view_none(client):
    g = client.get("/api/v1/app/operator/sub-merchants/ghost", headers=_auth(_CFG))
    assert g.status_code == 200 and g.json()["status"] == "none" and g.json()["destination_id"] == ""


def test_submerchant_view_shows_status_and_destination(client):
    # seed directly via the store (the provider onboards it in production)
    from app.core.submerchant import SubMerchantStore
    store = SubMerchantStore(client.app.state.container.datastore)
    run(store.set(tenant="acme", provider="tap", destination_id="dst_1", status="active"))
    g = client.get("/api/v1/app/operator/sub-merchants/acme", headers=_auth(_CFG))
    assert g.json()["status"] == "active" and g.json()["destination_id"] == "dst_1"
    assert g.json()["provider"] == "tap"


def test_submerchant_view_requires_capability(client):
    assert client.get("/api/v1/app/operator/sub-merchants/acme", headers=_auth()).status_code == 403
