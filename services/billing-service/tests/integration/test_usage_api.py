"""Usage/entitlement HTTP surface.

The headline test: a tenant can no longer state its own usage. That query parameter was the
whole limit — pass `current=0` and `seats: 25` allowed everything (CWE-602).
"""
from __future__ import annotations

import os
import time

import jwt
import pytest
from fastapi.testclient import TestClient

SECRET = "s" * 40
INTERNAL = "t" * 40


@pytest.fixture(scope="module")
def client():
    from app.config import get_settings

    saved = dict(os.environ)
    os.environ.update(ENV="test", INTERNAL_SERVICE_TOKEN=INTERNAL, TOKEN_VERIFIER_DRIVER="hs256",
                      TOKEN_HS256_SECRET=SECRET, TOKEN_ISSUER="i", TOKEN_AUDIENCE="a",
                      RATELIMIT_MAX="100000")
    get_settings.cache_clear()
    try:
        from app.main import create_app

        with TestClient(create_app()) as c:
            yield c
    finally:
        os.environ.clear()
        os.environ.update(saved)
        get_settings.cache_clear()


def tok(caps, tenant="t1"):
    claims = {"sub": "u1", "caps": caps, "tenant": tenant, "iss": "i", "aud": "a",
              "exp": int(time.time()) + 600}
    return {"Authorization": "Bearer " + jwt.encode(claims, SECRET, algorithm="HS256")}


IT = {"X-Internal-Token": INTERNAL}
ADMIN = ["platform.billing"]
SELF = ["billing.self"]


TENANTS = ("t1", "t_enf", "t_idem", "t_rel", "t_g")


@pytest.fixture(scope="module")
def subscribed(client):
    """Tenants on a plan with a counter, a gauge and a boolean.

    Every tenant used below must actually be subscribed: with no subscription there are no
    limits, and deny-by-default denies everything (asserted separately).
    """
    limits = {"sso": True,
              "seats": 3,
              "api_calls": {"kind": "counter", "limit": 5, "period": "month", "overage": "block"}}
    plan = client.post("/api/v1/billing/plans",
                       json={"name": "Pro", "prices": [{"currency": "SAR", "amount_minor": 1000,
                                                        "interval": "month"}],
                             "limits": limits},
                       headers=tok(ADMIN)).json()
    for tenant in TENANTS:
        client.post("/api/v1/billing/subscriptions",
                    json={"tenant": tenant, "plan_id": plan["id"]}, headers=tok(ADMIN))
    return plan


def test_a_tenant_with_no_subscription_is_entitled_to_nothing(client):
    """No plan ⇒ no limits ⇒ deny. An unsubscribed tenant must not inherit anything."""
    r = client.post("/internal/billing/usage/consume",
                    json={"tenant": "never_subscribed", "key": "api_calls"}, headers=IT)
    assert r.json()["allowed"] is False
    assert client.get("/internal/billing/entitlements/check?tenant=never_subscribed&key=sso",
                      headers=IT).json()["allowed"] is False


def test_the_entitlement_route_no_longer_accepts_a_usage_number(client, subscribed):
    """The fix. `current=0` used to make every limit pass."""
    for _ in range(5):
        client.post("/internal/billing/usage/consume",
                    json={"tenant": "t1", "key": "api_calls"}, headers=IT)

    # A tenant trying the old trick: claim zero usage.
    r = client.get("/api/v1/billing/entitlements/api_calls?current=0", headers=tok(SELF))
    assert r.status_code == 200
    body = r.json()
    assert body["current"] == 5, "the server must use ITS OWN usage number"
    assert body["allowed"] is False, "a client-supplied `current` still bypassed the limit"


def test_consume_enforces_and_then_denies(client, subscribed):
    for i in range(5):
        r = client.post("/internal/billing/usage/consume",
                        json={"tenant": "t_enf", "key": "api_calls"}, headers=IT)
        assert r.json()["allowed"] is True, f"call {i} denied early"
    r = client.post("/internal/billing/usage/consume",
                    json={"tenant": "t_enf", "key": "api_calls"}, headers=IT)
    assert r.json()["allowed"] is False and r.json()["remaining"] == 0


def test_consume_is_idempotent_over_http(client, subscribed):
    body = {"tenant": "t_idem", "key": "api_calls", "idempotency_key": "evt-42"}
    first = client.post("/internal/billing/usage/consume", json=body, headers=IT).json()
    again = client.post("/internal/billing/usage/consume", json=body, headers=IT).json()
    assert first["current"] == 1
    assert again["current"] == 1 and again["idempotent"] is True


def test_release_returns_a_unit(client, subscribed):
    client.post("/internal/billing/usage/consume",
                json={"tenant": "t_rel", "key": "api_calls"}, headers=IT)
    r = client.post("/internal/billing/usage/release",
                    json={"tenant": "t_rel", "key": "api_calls"}, headers=IT)
    assert r.json()["current"] == 0


def test_a_gauge_is_set_not_consumed(client, subscribed):
    bad = client.post("/internal/billing/usage/consume",
                      json={"tenant": "t_g", "key": "seats"}, headers=IT).json()
    assert bad["allowed"] is False and bad["error"] == "GAUGE_NOT_CONSUMABLE"

    client.post("/internal/billing/usage/set",
                json={"tenant": "t_g", "key": "seats", "value": 3}, headers=IT)
    assert client.get("/internal/billing/entitlements/check?tenant=t_g&key=seats",
                      headers=IT).json()["allowed"] is False

    client.post("/internal/billing/usage/set",           # a user was deleted
                json={"tenant": "t_g", "key": "seats", "value": 2}, headers=IT)
    assert client.get("/internal/billing/entitlements/check?tenant=t_g&key=seats",
                      headers=IT).json()["allowed"] is True


def test_usage_summary_shows_remaining(client, subscribed):
    client.post("/internal/billing/usage/set",
                json={"tenant": "t1", "key": "seats", "value": 2}, headers=IT)
    ents = client.get("/api/v1/billing/usage", headers=tok(SELF)).json()["entitlements"]
    assert ents["seats"]["current"] == 2 and ents["seats"]["remaining"] == 1


def test_usage_endpoints_require_the_internal_token(client):
    for path in ("/internal/billing/usage/consume", "/internal/billing/usage/release",
                 "/internal/billing/usage/set"):
        assert client.post(path, json={"tenant": "t", "key": "k", "value": 1}).status_code == 401


def test_a_user_token_cannot_reach_the_enforcement_tier(client):
    r = client.post("/internal/billing/usage/consume",
                    json={"tenant": "t1", "key": "api_calls"}, headers=tok(ADMIN))
    assert r.status_code == 401


def test_a_tenant_cannot_read_another_tenants_entitlements(client, subscribed):
    r = client.get("/api/v1/billing/usage", headers=tok(SELF, tenant="attacker"))
    assert r.json()["tenant"] == "attacker"       # pinned to the token, never a parameter


def test_unknown_entitlement_key_is_denied(client, subscribed):
    r = client.get("/api/v1/billing/entitlements/not_in_the_plan", headers=tok(SELF))
    assert r.json()["allowed"] is False


def test_boolean_entitlement_still_answers_for_feature_flag_service(client, subscribed):
    r = client.get("/internal/billing/entitlements/check?tenant=t1&key=sso", headers=IT)
    assert r.status_code == 200 and r.json()["allowed"] is True


def test_manifest_advertises_the_enforcement_endpoint(client):
    m = client.get("/internal/manifest", headers=IT).json()
    paths = {e["path"] for e in m["endpoints"]}
    assert "/internal/billing/usage/consume" in paths
