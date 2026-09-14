"""The internal coupon primitives a peer service (operations) uses to grant a subscription discount:

  POST /internal/billing/coupons                       — create ONE coupon, always private
  POST /internal/billing/subscriptions/{id}/coupon     — attach/replace a coupon post-signup

Security surface under test: internal-token only; `public` can never be set on the scoped create;
the percent cap is enforced; attach is tenant-guarded (IDOR) and idempotent (no double-redeem).
"""
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
                      TOKEN_HS256_SECRET="s" * 40, RATELIMIT_MAX="100000")
    get_settings.cache_clear()
    try:
        from app.main import create_app
        with TestClient(create_app()) as c:
            yield c
    finally:
        os.environ.clear()
        os.environ.update(saved)
        get_settings.cache_clear()


def _seed_plan(client, plan_id="pro"):
    ds = client.app.state.container.datastore
    run(ds.put("plans", plan_id, {
        "id": plan_id, "name": "Pro",
        "prices": [{"interval": "month", "amount_minor": 10000, "currency": "SAR", "type": "flat"}],
        "features": [], "limits": {}, "trial_days": 0, "active": True, "listed": True,
        "credit_percent_of_price": False}))
    return plan_id


def _subscribe(client, tenant="t1", plan_id="pro"):
    r = client.post("/internal/billing/subscriptions", headers=IT,
                    json={"tenant": tenant, "plan_id": plan_id, "payment_token": "pm"})
    assert r.status_code == 201, r.text
    return r.json()["id"]


# ── auth ─────────────────────────────────────────────────────────────────────
def test_both_routes_require_the_internal_token(client):
    assert client.post("/internal/billing/coupons",
                       json={"code": "X", "type": "percent", "value": 10}).status_code == 401
    assert client.post("/internal/billing/subscriptions/whatever/coupon",
                       json={"tenant": "t1", "coupon_code": "X"}).status_code == 401


# ── scoped create ────────────────────────────────────────────────────────────
def test_scoped_create_forces_public_false_even_if_sent(client):
    # `public` is not in the model, so an attempt to smuggle it is simply ignored — the coupon is
    # created private, and can never reach the unauthenticated /offers list.
    r = client.post("/internal/billing/coupons", headers=IT,
                    json={"code": "GRANT1", "type": "percent", "value": 25, "public": True})
    assert r.status_code == 201, r.text
    assert r.json()["public"] is False


def test_scoped_create_rejects_a_percent_over_100(client):
    r = client.post("/internal/billing/coupons", headers=IT,
                    json={"code": "TOOBIG", "type": "percent", "value": 150})
    assert r.status_code == 400 and r.json()["error"] == "BAD_REQUEST"


# ── attach ───────────────────────────────────────────────────────────────────
def test_attach_applies_a_coupon_and_is_idempotent(client):
    _seed_plan(client)
    sub_id = _subscribe(client, tenant="t1")
    client.post("/internal/billing/coupons", headers=IT,
                json={"code": "ATT20", "type": "percent", "value": 20, "max_redemptions": 1})
    r = client.post(f"/internal/billing/subscriptions/{sub_id}/coupon", headers=IT,
                    json={"tenant": "t1", "coupon_code": "ATT20"})
    assert r.status_code == 200 and r.json()["status"] == "attached"
    # retry (same request) is a no-op — no second redemption
    again = client.post(f"/internal/billing/subscriptions/{sub_id}/coupon", headers=IT,
                        json={"tenant": "t1", "coupon_code": "ATT20"})
    assert again.status_code == 200 and again.json()["status"] == "unchanged"
    ds = client.app.state.container.datastore
    assert int(run(ds.get("coupons", "ATT20"))["redeemed"]) == 1     # exactly one redemption


def test_attach_is_tenant_guarded(client):
    _seed_plan(client)
    sub_id = _subscribe(client, tenant="t1")
    client.post("/internal/billing/coupons", headers=IT,
                json={"code": "IDOR", "type": "percent", "value": 20})
    # another tenant naming a real subscription id gets 404, never a mutation
    r = client.post(f"/internal/billing/subscriptions/{sub_id}/coupon", headers=IT,
                    json={"tenant": "attacker", "coupon_code": "IDOR"})
    assert r.status_code == 404
    ds = client.app.state.container.datastore
    assert run(ds.get("subscriptions", sub_id)).get("coupon_code", "") == ""


def test_attach_fails_closed_on_an_expired_coupon(client):
    _seed_plan(client)
    sub_id = _subscribe(client, tenant="t1")
    client.post("/internal/billing/coupons", headers=IT,
                json={"code": "GONE", "type": "percent", "value": 20, "expires_at": 1.0})
    r = client.post(f"/internal/billing/subscriptions/{sub_id}/coupon", headers=IT,
                    json={"tenant": "t1", "coupon_code": "GONE"})
    assert r.status_code == 400 and r.json()["error"] == "BAD_REQUEST"
