"""Phase 1 — the marketplace vertical over real HTTP routes (Tap mocked at the http-helper boundary).

Drives the whole operator/consumer path: set platform MARKETPLACE_* creds (write-only), onboard a
sub-merchant, switch the tenant to marketplace mode, and mint a SPLIT pay-link — asserting write-only
credentials, the onboarding event, and fail-closed activation (no mode/creds/sub-merchant → refused).
"""
from __future__ import annotations

import asyncio
import os

import pytest
from fastapi.testclient import TestClient

import app.adapters.payment.tap as tap

ITOKEN = "t" * 40
KEK = "k" * 48


def run(c):
    return asyncio.run(c)


def _make_app() -> TestClient:
    from app.config import get_settings
    os.environ.update({
        "ENV": "test", "INTERNAL_SERVICE_TOKEN": ITOKEN,
        "TOKEN_VERIFIER_DRIVER": "hs256", "TOKEN_HS256_SECRET": "h" * 40,
        "RATELIMIT_MAX": "1000", "SECRETS_KEK": KEK,
        "PAYMENTS_REDIRECT_BASE_URL": "https://conduit.example",
        "PAYMENTS_REDIRECT_ALLOWED_HOSTS": "pay.tap.company",
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


def _h():
    return {"X-Internal-Token": ITOKEN}


def _configure_marketplace(client):
    for name, value in (("MARKETPLACE_PROVIDER", "tap"), ("MARKETPLACE_SECRET_KEY", "sk_test_123")):
        r = client.post("/internal/payments/marketplace-credentials", headers=_h(),
                        json={"name": name, "value": value})
        assert r.status_code == 200, r.text


# ── marketplace credentials are write-only ────────────────────────────────────────
def test_marketplace_credentials_are_write_only(client):
    _configure_marketplace(client)
    r = client.get("/internal/payments/marketplace-credentials", headers=_h())
    assert r.status_code == 200
    names = {c["name"]: c for c in r.json()["credentials"]}
    assert "MARKETPLACE_SECRET_KEY" in names
    # the hint never reveals the value
    assert "sk_test_123" not in str(r.json())


def test_unknown_marketplace_credential_refused(client):
    r = client.post("/internal/payments/marketplace-credentials", headers=_h(),
                    json={"name": "MARKETPLACE_ROOT", "value": "x"})
    assert r.status_code == 422


# ── onboarding (api) emits the event + stores the mapping ──────────────────────────
def test_set_destination_stores_and_emits_event(client):
    # The destination is recorded post-approval (Tap lead approved / Moyasar dashboard recipient) via the
    # destination route, which stores the mapping and emits merchant.onboarding_updated.
    _configure_marketplace(client)   # tap
    events = []

    async def collect(ev):
        events.append(ev)
    run(client.app.state.container.event_bus.subscribe("merchant.onboarding_updated", collect))

    r = client.post("/internal/payments/sub-merchants/destination", headers=_h(),
                    json={"tenant": "acme", "destination_id": "dst_1", "status": "active"})
    assert r.status_code == 200 and r.json()["destination_id"] == "dst_1"
    g = client.get("/internal/payments/sub-merchants/acme", headers=_h()).json()
    assert g["status"] == "active" and g["destination_id"] == "dst_1"
    assert any(e.type == "merchant.onboarding_updated" and e.payload["status"] == "active"
               and e.payload["has_destination"] for e in events)


def test_onboard_api_creates_a_lead_pending(client, monkeypatch):
    # Tap api onboarding creates a v3 lead (pending) — no destination yet; verified by the adapter tests.
    _configure_marketplace(client)   # tap
    monkeypatch.setattr(tap, "post_json", lambda *a, **k: (200, {"id": "led_1", "status": "created"}))
    r = client.post("/internal/payments/sub-merchants/onboard", headers=_h(),
                    json={"tenant": "acme", "mode": "api", "data": {"brand": {"name": "Acme"}}})
    assert r.status_code == 200 and r.json()["status"] == "pending" and r.json()["destination_id"] == ""


def test_onboard_refused_when_marketplace_unconfigured(client):
    # no marketplace creds set → onboarding has no provider → 409
    r = client.post("/internal/payments/sub-merchants/onboard", headers=_h(),
                    json={"tenant": "acme", "mode": "api", "data": {}})
    assert r.status_code == 409


def test_onboard_unknown_mode_refused(client):
    _configure_marketplace(client)
    r = client.post("/internal/payments/sub-merchants/onboard", headers=_h(),
                    json={"tenant": "acme", "mode": "telepathy", "data": {}})
    assert r.status_code == 422


# ── mode selection + split checkout end to end ─────────────────────────────────────
def _onboard_and_mode(client, monkeypatch):
    _configure_marketplace(client)   # tap
    # Tap onboarding is lead-based (pending); the usable destination lands post-approval via the
    # destination route — seed that here, then switch the tenant to marketplace mode.
    client.post("/internal/payments/sub-merchants/destination", headers=_h(),
                json={"tenant": "acme", "destination_id": "dst_acme", "status": "active"})
    r = client.post("/internal/payments/payment-mode", headers=_h(),
                    json={"tenant": "acme", "mode": "marketplace"})
    assert r.status_code == 200 and r.json()["mode"] == "marketplace"


def test_split_checkout_via_route(client, monkeypatch):
    _onboard_and_mode(client, monkeypatch)
    # now the charge call returns a hosted url
    monkeypatch.setattr(tap, "post_json",
                        lambda *a, **k: (200, {"id": "chg_1", "transaction": {"url": "https://pay.tap.company/x"}}))
    r = client.post("/internal/payments/requests", headers=_h(),
                    json={"tenant": "acme", "reference": "o1", "amount_minor": 10000, "currency": "SAR",
                          "merchant_amount_minor": 9000,
                          "metadata": {"customerName": "A", "customerEmail": "a@b.c"}})
    assert r.status_code == 200, r.text
    assert r.json()["provider"] == "tap" and r.json()["provider_ref"] == "chg_1"


def test_marketplace_mode_without_submerchant_is_refused(client, monkeypatch):
    _configure_marketplace(client)
    client.post("/internal/payments/payment-mode", headers=_h(),
                json={"tenant": "acme", "mode": "marketplace"})
    # no sub-merchant onboarded → split cannot route → 409 NOT_CONFIGURED
    r = client.post("/internal/payments/requests", headers=_h(),
                    json={"tenant": "acme", "reference": "o2", "amount_minor": 10000, "currency": "SAR",
                          "merchant_amount_minor": 9000,
                          "metadata": {"customerName": "A", "customerEmail": "a@b.c"}})
    assert r.status_code == 409


def test_unknown_mode_refused_via_route(client):
    r = client.post("/internal/payments/payment-mode", headers=_h(),
                    json={"tenant": "acme", "mode": "crypto"})
    assert r.status_code == 422
