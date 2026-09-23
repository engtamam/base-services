"""POST /internal/billing/subscriptions/checkout-link — the internal (service-to-service) twin of the
hosted-checkout subscription link. Same core .start_subscription_checkout(); tenant in the body;
X-Internal-Token auth. A product service (e.g. operations) uses this rather than a personal user
bearer, exactly like subscribe_internal / checkout_internal."""
from __future__ import annotations

import asyncio
import os

import pytest
from fastapi.testclient import TestClient

from app.ports.payment_gateway import CheckoutSession

INTERNAL = "t" * 40
IT = {"X-Internal-Token": INTERNAL}


def run(c):
    return asyncio.run(c)


class _HostedGW:
    driver = "hosted"
    supports_token_charge = False

    def prepare_checkout(self, *, amount_minor, currency, reference, return_url="", metadata=None):
        return CheckoutSession(ok=True, provider="hosted", id=f"sess_{reference}",
                               redirect_url=f"https://checkout.hosted/pay/{reference}")

    def fetch_status(self, *, provider_id, currency):
        class _S:
            status = "pending"
        return _S()


@pytest.fixture(scope="module")
def client():
    from app.config import get_settings
    saved = dict(os.environ)
    os.environ.update(ENV="test", INTERNAL_SERVICE_TOKEN=INTERNAL, TOKEN_VERIFIER_DRIVER="hs256",
                      TOKEN_HS256_SECRET="s" * 40, RATELIMIT_MAX="100000", TRIAL_DAYS="0",
                      BILLING_PAY_LINK_BASE_URL="https://pay.example.com/l")
    get_settings.cache_clear()
    try:
        from app.main import create_app
        app = create_app()
        with TestClient(app) as c:
            c.app.state.container.payment_gateway = _HostedGW()   # hosted-checkout gateway
            yield c
    finally:
        os.environ.clear()
        os.environ.update(saved)
        get_settings.cache_clear()


def _seed_plan(client, plan_id, amount=10000, trial_days=0):
    ds = client.app.state.container.datastore
    run(ds.put("plans", plan_id, {
        "id": plan_id, "name": plan_id.title(),
        "prices": [{"interval": "month", "amount_minor": amount, "currency": "SAR", "type": "flat"}],
        "features": [], "limits": {}, "trial_days": trial_days, "active": True, "listed": True,
        "credit_percent_of_price": False}))
    return plan_id


def test_requires_the_internal_token(client):
    _seed_plan(client, "pl_auth")
    r = client.post("/internal/billing/subscriptions/checkout-link",
                    json={"tenant": "t1", "plan_id": "pl_auth"})
    assert r.status_code in (401, 403)                       # no token → refused


def test_creates_an_incomplete_sub_and_returns_a_link_for_the_body_tenant(client):
    _seed_plan(client, "pl_a", 10000)
    r = client.post("/internal/billing/subscriptions/checkout-link",
                    json={"tenant": "shopA", "plan_id": "pl_a", "idempotency_key": "k1"}, headers=IT)
    assert r.status_code == 201, r.text
    body = r.json()
    assert body["status"] == "incomplete" and body["checkout_required"] is True
    assert body["amount_minor"] == 11500 and body["currency"] == "SAR"
    assert body["checkout_url"].startswith("https://checkout.hosted/pay/")
    assert body["pay_url"].startswith("https://pay.example.com/l/")
    # the subscription belongs to the BODY tenant, not any principal
    ds = client.app.state.container.datastore
    sub = run(ds.get("subscriptions", body["subscription_id"]))
    assert sub["tenant"] == "shopA" and sub["status"] == "incomplete"


def test_a_retry_replays_the_same_link(client):
    _seed_plan(client, "pl_b", 10000)
    a = client.post("/internal/billing/subscriptions/checkout-link",
                    json={"tenant": "shopB", "plan_id": "pl_b", "idempotency_key": "kb"}, headers=IT).json()
    b = client.post("/internal/billing/subscriptions/checkout-link",
                    json={"tenant": "shopB", "plan_id": "pl_b", "idempotency_key": "kb"}, headers=IT).json()
    assert b["subscription_id"] == a["subscription_id"] and b["token"] == a["token"]


def test_a_trial_plan_needs_no_checkout(client):
    _seed_plan(client, "pl_trial", 10000, trial_days=14)
    r = client.post("/internal/billing/subscriptions/checkout-link",
                    json={"tenant": "shopT", "plan_id": "pl_trial", "idempotency_key": "kt"}, headers=IT)
    assert r.status_code == 201, r.text
    body = r.json()
    assert body["checkout_required"] is False and body["status"] == "trialing"


def test_unknown_plan_is_refused_422(client):
    r = client.post("/internal/billing/subscriptions/checkout-link",
                    json={"tenant": "shopX", "plan_id": "nope"}, headers=IT)
    assert r.status_code == 422
