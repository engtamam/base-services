"""POST /internal/billing/subscriptions/checkout — the internal (service-to-service) twin of the
multi-plan cart checkout. Same core .checkout(); tenant in the body; X-Internal-Token auth."""
from __future__ import annotations

import asyncio
import os

import pytest
from fastapi.testclient import TestClient

INTERNAL = "t" * 40
IT = {"X-Internal-Token": INTERNAL}


def run(c):
    return asyncio.run(c)


@pytest.fixture(scope="module")
def client():
    from app.config import get_settings
    saved = dict(os.environ)
    os.environ.update(ENV="test", INTERNAL_SERVICE_TOKEN=INTERNAL, TOKEN_VERIFIER_DRIVER="hs256",
                      TOKEN_HS256_SECRET="s" * 40, RATELIMIT_MAX="100000", TRIAL_DAYS="0")
    get_settings.cache_clear()
    try:
        from app.main import create_app
        with TestClient(create_app()) as c:
            yield c
    finally:
        os.environ.clear()
        os.environ.update(saved)
        get_settings.cache_clear()


def _seed_plan(client, plan_id, amount=10000):
    ds = client.app.state.container.datastore
    run(ds.put("plans", plan_id, {
        "id": plan_id, "name": plan_id.title(),
        "prices": [{"interval": "month", "amount_minor": amount, "currency": "SAR", "type": "flat"}],
        "features": [], "limits": {}, "trial_days": 0, "active": True, "listed": True,
        "credit_percent_of_price": False}))
    return plan_id


def _invoices(client, tenant):
    ds = client.app.state.container.datastore
    rows, _ = run(ds.find("invoices", {"tenant": tenant}, limit=100))
    return rows


def test_requires_the_internal_token(client):
    _seed_plan(client, "p_auth")
    r = client.post("/internal/billing/subscriptions/checkout",
                    json={"tenant": "t1", "items": [{"plan_id": "p_auth"}], "payment_token": "pm"})
    assert r.status_code in (401, 403)                      # no token → refused


def test_internal_checkout_creates_one_invoice_for_the_body_tenant(client):
    _seed_plan(client, "p_a", 10000); _seed_plan(client, "p_b", 5000)
    r = client.post("/internal/billing/subscriptions/checkout", headers=IT,
                    json={"tenant": "shopX", "items": [{"plan_id": "p_a"}, {"plan_id": "p_b"}],
                          "payment_token": "pm"})
    assert r.status_code == 201, r.text
    body = r.json()
    assert body["tenant"] == "shopX"                        # tenant from the body, not a principal
    assert {i["plan_id"] for i in body["items"]} == {"p_a", "p_b"}
    assert body["total_minor"] == 17250                     # (10000+5000)+15% VAT
    assert len(_invoices(client, "shopX")) == 1             # ONE invoice for the cart


def test_internal_checkout_is_atomic_bad_cart_creates_nothing(client):
    _seed_plan(client, "p_ok")
    r = client.post("/internal/billing/subscriptions/checkout", headers=IT,
                    json={"tenant": "shopBad", "items": [{"plan_id": "p_ok"}, {"plan_id": "ghost"}],
                          "payment_token": "pm"})
    assert r.status_code == 422
    assert _invoices(client, "shopBad") == []              # whole cart refused, nothing created


def test_internal_checkout_is_idempotent(client):
    _seed_plan(client, "p_idem")
    payload = {"tenant": "shopIdem", "items": [{"plan_id": "p_idem"}], "payment_token": "pm",
               "idempotency_key": "cart-77"}
    first = client.post("/internal/billing/subscriptions/checkout", headers=IT, json=payload)
    second = client.post("/internal/billing/subscriptions/checkout", headers=IT, json=payload)
    assert first.status_code == 201 and second.status_code == 201
    assert first.json() == second.json()                   # replayed, not a second cart
    assert len(_invoices(client, "shopIdem")) == 1
