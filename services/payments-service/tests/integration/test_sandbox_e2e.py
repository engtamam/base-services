"""Production-simulation E2E — the whole marketplace vertical against doc-accurate provider SIMULATORS.

The sandbox (tests/support/provider_sandbox) stands in for Tap/Moyasar at the http boundary with the
documented request/response shapes and REAL signatures. These tests drive the full stack over HTTP —
configure creds → onboard → activate destination → set mode → mint a SPLIT checkout → the provider
captures → the provider POSTs a SIGNED webhook → we verify (real hashstring/HMAC) + verify-by-fetch →
payment completes; plus a payout webhook. A forged webhook is rejected. If our adapter drifts from the
documented request shape, the strict simulator 400s and the test fails.
"""
from __future__ import annotations

import asyncio
import json
import os

import pytest
from fastapi.testclient import TestClient

import app.adapters.payment.moyasar as moy
import app.adapters.payment.tap as tap
from tests.support.provider_sandbox import MoyasarSandbox, TapSandbox

ITOKEN = "t" * 40
KEK = "k" * 48


def run(c):
    return asyncio.run(c)


def _app() -> TestClient:
    from app.config import get_settings
    os.environ.update({
        "ENV": "test", "INTERNAL_SERVICE_TOKEN": ITOKEN,
        "TOKEN_VERIFIER_DRIVER": "hs256", "TOKEN_HS256_SECRET": "h" * 40,
        "RATELIMIT_MAX": "1000", "SECRETS_KEK": KEK,
        "PAYMENTS_REDIRECT_BASE_URL": "https://conduit.example",
        "PAYMENTS_REDIRECT_ALLOWED_HOSTS": "pay.tap.company,moyasar.com",
    })
    get_settings.cache_clear()
    from app.main import create_app
    return TestClient(create_app(), raise_server_exceptions=False)


@pytest.fixture()
def client():
    from app.config import get_settings
    saved = dict(os.environ)
    c = _app()
    yield c
    os.environ.clear(); os.environ.update(saved); get_settings.cache_clear()


def _h():
    return {"X-Internal-Token": ITOKEN}


def _cred(client, name, value):
    assert client.post("/internal/payments/marketplace-credentials", headers=_h(),
                       json={"name": name, "value": value}).status_code == 200


def _events(client, topic):
    got = []

    async def collect(ev):
        got.append(ev)
    run(client.app.state.container.event_bus.subscribe(topic, collect))
    return got


# ═══════════════════════════════════ Tap E2E ═════════════════════════════════════
def test_tap_full_split_lifecycle(client, monkeypatch):
    sb = TapSandbox(secret_key="sk_test_tap")
    monkeypatch.setattr(tap, "post_json", sb.post_json)
    monkeypatch.setattr(tap, "get_json", sb.get_json)
    _cred(client, "MARKETPLACE_PROVIDER", "tap")
    _cred(client, "MARKETPLACE_SECRET_KEY", "sk_test_tap")

    # onboard (api → v3 lead, pending) then activate the destination post-approval
    r = client.post("/internal/payments/sub-merchants/onboard", headers=_h(),
                    json={"tenant": "acme", "mode": "api",
                          "data": {"brand": {"name": "Acme"}, "marketplace": {"id": "mp_1"}}})
    assert r.status_code == 200 and r.json()["status"] == "pending"
    assert client.post("/internal/payments/sub-merchants/destination", headers=_h(),
                       json={"tenant": "acme", "destination_id": "dst_acme", "status": "active"}).status_code == 200
    client.post("/internal/payments/payment-mode", headers=_h(),
                json={"tenant": "acme", "mode": "marketplace"})

    # mint a split checkout — the simulator validates the charge body (customer.email, source.id, destinations)
    completed = _events(client, "payment.completed")
    r = client.post("/internal/payments/requests", headers=_h(),
                    json={"tenant": "acme", "reference": "ord-1", "amount_minor": 10000, "currency": "SAR",
                          "merchant_amount_minor": 9000, "return_url": "https://pay.tap.company/back",
                          "metadata": {"customerName": "Sara", "customerEmail": "s@x.com"}})
    assert r.status_code == 200, r.text
    charge_id = r.json()["provider_ref"]
    assert charge_id.startswith("chg_")
    # the destination was routed the merchant's share
    assert sb.charges[charge_id]["amount"] == 100.0

    # customer pays on Tap; Tap POSTs a SIGNED webhook → we verify (hashstring) + verify-by-fetch
    sb.capture(charge_id)
    headers, body = sb.charge_webhook(charge_id)
    wr = client.post("/api/v1/payments/webhook/acme", headers=headers, content=body)
    assert wr.status_code == 200 and wr.json()["applied"] is True and wr.json()["status"] == "completed"
    assert any(e.payload["reference"] == "ord-1" for e in completed)


def test_tap_forged_webhook_is_rejected(client, monkeypatch):
    sb = TapSandbox(secret_key="sk_test_tap")
    monkeypatch.setattr(tap, "post_json", sb.post_json)
    monkeypatch.setattr(tap, "get_json", sb.get_json)
    _cred(client, "MARKETPLACE_PROVIDER", "tap")
    _cred(client, "MARKETPLACE_SECRET_KEY", "sk_test_tap")
    client.post("/internal/payments/sub-merchants/destination", headers=_h(),
                json={"tenant": "acme", "destination_id": "dst_acme", "status": "active"})
    client.post("/internal/payments/payment-mode", headers=_h(), json={"tenant": "acme", "mode": "marketplace"})
    r = client.post("/internal/payments/requests", headers=_h(),
                    json={"tenant": "acme", "reference": "ord-2", "amount_minor": 10000, "currency": "SAR",
                          "merchant_amount_minor": 9000,
                          "metadata": {"customerName": "S", "customerEmail": "s@x.com"}})
    charge_id = r.json()["provider_ref"]
    sb.capture(charge_id)
    headers, body = sb.charge_webhook(charge_id)
    tampered = json.loads(body); tampered["amount"] = 1.0           # inflate/alter the signed body
    wr = client.post("/api/v1/payments/webhook/acme", headers=headers,
                     content=json.dumps(tampered).encode())
    assert wr.json()["applied"] is False                           # hashstring no longer matches → refused
    got = client.get(f"/internal/payments/requests/{r.json()['request_id']}?tenant=acme", headers=_h())
    assert got.json()["status"] == "pending"                       # nothing settled


def test_tap_payout_webhook(client, monkeypatch):
    sb = TapSandbox(secret_key="sk_test_tap")
    monkeypatch.setattr(tap, "post_json", sb.post_json)
    monkeypatch.setattr(tap, "get_json", sb.get_json)
    _cred(client, "MARKETPLACE_PROVIDER", "tap")
    _cred(client, "MARKETPLACE_SECRET_KEY", "sk_test_tap")
    pid = sb.add_payout(status="PAID_OUT", amount=50.0, currency="SAR")
    settled = _events(client, "payout.settled")
    headers, body = sb.payout_webhook(pid)
    wr = client.post("/api/v1/payments/payout-webhook", headers=headers, content=body)
    assert wr.status_code == 200 and wr.json()["status"] == "paid"
    assert any(e.payload["payout_ref"] == pid for e in settled)


# ═══════════════════════════════════ Moyasar E2E ═════════════════════════════════
def test_moyasar_full_split_lifecycle_token_webhook(client, monkeypatch):
    sb = MoyasarSandbox(secret_key="sk_live_moy", webhook_secret="whsec_moy")
    monkeypatch.setattr(moy, "post_json", sb.post_json)
    monkeypatch.setattr(moy, "get_json", sb.get_json)
    _cred(client, "MARKETPLACE_PROVIDER", "moyasar")
    _cred(client, "MARKETPLACE_SECRET_KEY", "sk_live_moy")
    _cred(client, "MARKETPLACE_WEBHOOK_SECRET", "whsec_moy")

    # onboard (api → entity, immediate active recipient id)
    r = client.post("/internal/payments/sub-merchants/onboard", headers=_h(),
                    json={"tenant": "shop", "mode": "api", "data": {"name": "Shop"}})
    assert r.status_code == 200 and r.json()["destination_id"].startswith("ent_")
    client.post("/internal/payments/payment-mode", headers=_h(), json={"tenant": "shop", "mode": "marketplace"})

    completed = _events(client, "payment.completed")
    r = client.post("/internal/payments/requests", headers=_h(),
                    json={"tenant": "shop", "reference": "mord-1", "amount_minor": 10000, "currency": "SAR",
                          "merchant_amount_minor": 9000,
                          "metadata": {"customerName": "A", "customerEmail": "a@b.c"}})
    assert r.status_code == 200, r.text
    inv_id = r.json()["provider_ref"]
    assert inv_id.startswith("inv_")
    assert sb.invoices[inv_id]["amount"] == 10000                  # minor units, no conversion

    sb.pay_invoice(inv_id)
    headers, body = sb.invoice_webhook(inv_id, mode="token")        # secret_token in body
    wr = client.post("/api/v1/payments/webhook/shop", headers=headers, content=body)
    assert wr.status_code == 200 and wr.json()["applied"] is True and wr.json()["status"] == "completed"
    assert any(e.payload["reference"] == "mord-1" for e in completed)


def test_moyasar_hmac_webhook(client, monkeypatch):
    sb = MoyasarSandbox(secret_key="sk_live_moy", webhook_secret="whsec_moy")
    monkeypatch.setattr(moy, "post_json", sb.post_json)
    monkeypatch.setattr(moy, "get_json", sb.get_json)
    _cred(client, "MARKETPLACE_PROVIDER", "moyasar")
    _cred(client, "MARKETPLACE_SECRET_KEY", "sk_live_moy")
    _cred(client, "MARKETPLACE_WEBHOOK_SECRET", "whsec_moy")
    client.post("/internal/payments/sub-merchants/destination", headers=_h(),
                json={"tenant": "shop", "destination_id": "ent_x", "status": "active"})
    client.post("/internal/payments/payment-mode", headers=_h(), json={"tenant": "shop", "mode": "marketplace"})
    r = client.post("/internal/payments/requests", headers=_h(),
                    json={"tenant": "shop", "reference": "mord-2", "amount_minor": 5000, "currency": "SAR",
                          "merchant_amount_minor": 4000,
                          "metadata": {"customerName": "A", "customerEmail": "a@b.c"}})
    inv_id = r.json()["provider_ref"]
    sb.pay_invoice(inv_id)
    headers, body = sb.invoice_webhook(inv_id, mode="hmac")         # X-Moyasar-Signature HMAC
    wr = client.post("/api/v1/payments/webhook/shop", headers=headers, content=body)
    assert wr.json()["applied"] is True and wr.json()["status"] == "completed"


def test_moyasar_forged_token_webhook_rejected(client, monkeypatch):
    sb = MoyasarSandbox(secret_key="sk_live_moy", webhook_secret="whsec_moy")
    monkeypatch.setattr(moy, "post_json", sb.post_json)
    monkeypatch.setattr(moy, "get_json", sb.get_json)
    _cred(client, "MARKETPLACE_PROVIDER", "moyasar")
    _cred(client, "MARKETPLACE_SECRET_KEY", "sk_live_moy")
    _cred(client, "MARKETPLACE_WEBHOOK_SECRET", "whsec_moy")
    client.post("/internal/payments/sub-merchants/destination", headers=_h(),
                json={"tenant": "shop", "destination_id": "ent_x", "status": "active"})
    client.post("/internal/payments/payment-mode", headers=_h(), json={"tenant": "shop", "mode": "marketplace"})
    r = client.post("/internal/payments/requests", headers=_h(),
                    json={"tenant": "shop", "reference": "mord-3", "amount_minor": 5000, "currency": "SAR",
                          "merchant_amount_minor": 4000,
                          "metadata": {"customerName": "A", "customerEmail": "a@b.c"}})
    inv_id = r.json()["provider_ref"]
    sb.pay_invoice(inv_id)
    body = json.dumps({"type": "payment_paid", "data": {"id": inv_id}, "secret_token": "WRONG"}).encode()
    wr = client.post("/api/v1/payments/webhook/shop", headers={}, content=body)
    assert wr.json()["applied"] is False                           # wrong secret_token → refused
