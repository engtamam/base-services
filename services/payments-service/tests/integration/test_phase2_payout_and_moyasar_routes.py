"""Phase 2 — payout webhook/sweep routes + a Moyasar-provider split flow (provider-neutral at the route).

The payout webhook is unauthenticated (the provider has no token) and settles only via verify-by-fetch;
the Moyasar flow proves the whole marketplace vertical runs on a second provider with no core change.
"""
from __future__ import annotations

import asyncio
import os

import pytest
from fastapi.testclient import TestClient


def run(c):
    return asyncio.run(c)

import app.adapters.payment.moyasar as moy
import app.adapters.payment.tap as tap

ITOKEN = "t" * 40
KEK = "k" * 48


def _make_app() -> TestClient:
    from app.config import get_settings
    os.environ.update({
        "ENV": "test", "INTERNAL_SERVICE_TOKEN": ITOKEN,
        "TOKEN_VERIFIER_DRIVER": "hs256", "TOKEN_HS256_SECRET": "h" * 40,
        "RATELIMIT_MAX": "1000", "SECRETS_KEK": KEK,
        "PAYMENTS_REDIRECT_BASE_URL": "https://conduit.example",
        "PAYMENTS_REDIRECT_ALLOWED_HOSTS": "pay.tap.company,moyasar.test",
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


def _set_provider(client, provider):
    for name, value in (("MARKETPLACE_PROVIDER", provider), ("MARKETPLACE_SECRET_KEY", "sk_x")):
        assert client.post("/internal/payments/marketplace-credentials", headers=_h(),
                           json={"name": name, "value": value}).status_code == 200


# ── payout webhook route ────────────────────────────────────────────────────────
def test_payout_webhook_applies_via_fetch(client, monkeypatch):
    _set_provider(client, "tap")
    monkeypatch.setattr(tap, "post_json",
                        lambda *a, **k: (200, {"payouts": [{"id": "po_1", "status": "PAID_OUT",
                                                            "amount": 50.0, "currency": "SAR"}]}))
    events = []

    async def collect(ev):
        events.append(ev)
    run(client.app.state.container.event_bus.subscribe("payout.settled", collect))

    r = client.post("/api/v1/payments/payout-webhook", json={"id": "po_1"})
    assert r.status_code == 200 and r.json()["applied"] is True and r.json()["status"] == "paid"
    assert any(e.type == "payout.settled" for e in events)


def test_payout_webhook_no_id_applies_nothing(client):
    _set_provider(client, "tap")
    r = client.post("/api/v1/payments/payout-webhook", json={"noise": 1})
    assert r.status_code == 200 and r.json()["applied"] is False


def test_payout_webhook_unreachable_asks_retry(client, monkeypatch):
    _set_provider(client, "tap")
    monkeypatch.setattr(tap, "post_json", lambda *a, **k: (0, {}))
    r = client.post("/api/v1/payments/payout-webhook", json={"id": "po_1"})
    assert r.status_code == 503                 # verification pending → provider retries


def test_payout_reconcile_sweep_route(client, monkeypatch):
    _set_provider(client, "tap")
    # seed a pending payout, then the sweep re-queries it to terminal
    from app.core.payout import PayoutStore
    store = PayoutStore(client.app.state.container.datastore)
    run(store.apply(payout_ref="po_9", status="pending"))
    monkeypatch.setattr(tap, "post_json",
                        lambda *a, **k: (200, {"payouts": [{"id": "po_9", "status": "PAID_OUT",
                                                            "amount": 10.0, "currency": "SAR"}]}))
    r = client.post("/internal/payments/payouts/reconcile-sweep", headers=_h())
    assert r.status_code == 200 and r.json()["reconciled"] >= 1


# ── Moyasar-provider split flow (provider-neutral at the route) ─────────────────────
def test_marketplace_split_flow_on_moyasar(client, monkeypatch):
    _set_provider(client, "moyasar")
    # Moyasar recipients are created in its dashboard (no API) — record the destination directly.
    r = client.post("/internal/payments/sub-merchants/destination", headers=_h(),
                    json={"tenant": "acme", "destination_id": "ent_1", "status": "active"})
    assert r.status_code == 200
    client.post("/internal/payments/payment-mode", headers=_h(),
                json={"tenant": "acme", "mode": "marketplace"})
    # split checkout → Moyasar invoice (minor units)
    captured = {}

    def fake_post(url, payload, headers, timeout=None):
        captured["payload"] = payload
        return 200, {"id": "inv_1", "url": "https://moyasar.test/x", "status": "initiated"}
    monkeypatch.setattr(moy, "post_json", fake_post)
    r = client.post("/internal/payments/requests", headers=_h(),
                    json={"tenant": "acme", "reference": "o1", "amount_minor": 10000, "currency": "SAR",
                          "merchant_amount_minor": 9000,
                          "metadata": {"customerName": "A", "customerEmail": "a@b.c"}})
    assert r.status_code == 200 and r.json()["provider"] == "moyasar"
    # minor-unit split routed to the tenant's onboarded recipient
    assert captured["payload"]["splits"] == [
        {"amount": 9000, "recipient_id": "ent_1", "recipient_type": "Entity"}]


def test_moyasar_hosted_onboarding_refused_at_route(client, monkeypatch):
    _set_provider(client, "moyasar")   # Moyasar declares api-only onboarding
    r = client.post("/internal/payments/sub-merchants/onboard", headers=_h(),
                    json={"tenant": "acme", "mode": "hosted", "data": {}})
    assert r.status_code == 409        # provider does not support this onboarding mode
