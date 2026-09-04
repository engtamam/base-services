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


def test_admin_usage_reads_a_named_tenants_limits(client, subscribed):
    # The platform-admin twin: a platform.billing admin from ANOTHER tenant reads t1's usage/limits.
    client.post("/internal/billing/usage/set",
                json={"tenant": "t1", "key": "seats", "value": 2}, headers=IT)
    r = client.get("/api/v1/billing/admin/usage?tenant=t1", headers=tok(ADMIN, tenant="ops"))
    assert r.status_code == 200
    assert r.json()["tenant"] == "t1"
    assert r.json()["entitlements"]["seats"]["current"] == 2      # the NAMED tenant's usage
    # identical to what t1 sees for itself via /usage
    own = client.get("/api/v1/billing/usage", headers=tok(SELF, tenant="t1")).json()
    assert r.json()["entitlements"]["seats"]["remaining"] == own["entitlements"]["seats"]["remaining"]


def test_admin_usage_requires_platform_billing_not_a_tenant_cap(client, subscribed):
    # A tenant admin cannot read ANOTHER tenant's usage — platform.billing only (never silently
    # their own rows, never someone else's): 403.
    r = client.get("/api/v1/billing/admin/usage?tenant=t1", headers=tok(SELF, tenant="attacker"))
    assert r.status_code == 403


def test_admin_usage_requires_a_tenant(client):
    assert client.get("/api/v1/billing/admin/usage", headers=tok(ADMIN)).status_code == 422


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


# ── internal subscriptions list (what a tenant HOLDS) ─────────────────────────
def test_internal_subscriptions_lists_what_a_tenant_holds(client, subscribed):
    r = client.get("/internal/billing/subscriptions?tenant=t1", headers=IT)
    assert r.status_code == 200
    subs = r.json()["subscriptions"]
    assert len(subs) >= 1
    s0 = subs[0]
    # EXACTLY the five fields the base-plus-apps rules need — and nothing else. The payment token and
    # the buyer's tax identity that live on the row must never cross the internal API.
    assert set(s0) == {"id", "plan_id", "status", "current_period_end", "limits_snapshot"}
    assert s0["plan_id"] == subscribed["id"] and isinstance(s0["limits_snapshot"], dict)


def test_internal_subscriptions_lists_every_held_item_not_just_one(client, subscribed):
    # base-plus-apps: a tenant holds MORE than one subscription and the union IS the entitlement, so
    # every held item must be listed — the whole reason a "has a base" boolean was not enough.
    app = client.post("/api/v1/billing/plans",
                      json={"name": "Extra App", "prices": [{"currency": "SAR", "amount_minor": 500,
                                                             "interval": "month"}]},
                      headers=tok(ADMIN)).json()
    client.post("/api/v1/billing/subscriptions",
                json={"tenant": "t_multi", "plan_id": subscribed["id"]}, headers=tok(ADMIN))
    client.post("/api/v1/billing/subscriptions",
                json={"tenant": "t_multi", "plan_id": app["id"]}, headers=tok(ADMIN))
    plans = {s["plan_id"] for s in client.get("/internal/billing/subscriptions?tenant=t_multi",
                                              headers=IT).json()["subscriptions"]}
    assert {subscribed["id"], app["id"]} <= plans


def test_internal_subscriptions_empty_for_an_unsubscribed_tenant(client):
    r = client.get("/internal/billing/subscriptions?tenant=never_subscribed_here", headers=IT)
    assert r.status_code == 200 and r.json()["subscriptions"] == []


def test_internal_subscriptions_requires_the_internal_token(client):
    assert client.get("/internal/billing/subscriptions?tenant=t1").status_code == 401
    # a user token is not a service — the internal tier refuses it
    assert client.get("/internal/billing/subscriptions?tenant=t1", headers=tok(ADMIN)).status_code == 401


def test_internal_subscriptions_requires_a_tenant(client):
    assert client.get("/internal/billing/subscriptions", headers=IT).status_code == 422


# ── internal plan/subscribe twins (a peer service, no per-pod IP bucket) ───────
def test_internal_plans_list_is_token_authed(client, subscribed):
    assert client.get("/internal/billing/plans").status_code == 401          # no token
    r = client.get("/internal/billing/plans", headers=IT)
    assert r.status_code == 200
    assert any(p["id"] == subscribed["id"] for p in r.json()["plans"])


def test_internal_subscribe_creates_a_subscription_with_tenant_in_the_body(client, subscribed):
    r = client.post("/internal/billing/subscriptions", headers=IT,
                    json={"tenant": "peer_shop", "plan_id": subscribed["id"]})
    assert r.status_code == 201, r.text
    body = r.json()
    assert body["tenant"] == "peer_shop" and body["plan_id"] == subscribed["id"]
    # and it is now the tenant's held set (the resolver's view agrees)
    held = client.get("/internal/billing/subscriptions?tenant=peer_shop", headers=IT).json()
    assert any(s["plan_id"] == subscribed["id"] for s in held["subscriptions"])


def test_internal_subscribe_requires_the_token_and_a_tenant(client, subscribed):
    assert client.post("/internal/billing/subscriptions",
                       json={"tenant": "x", "plan_id": subscribed["id"]}).status_code == 401
    assert client.post("/internal/billing/subscriptions", headers=IT,
                       json={"plan_id": subscribed["id"]}).status_code == 422   # tenant required


def test_internal_subscribe_unknown_plan_is_404(client):
    r = client.post("/internal/billing/subscriptions", headers=IT,
                    json={"tenant": "t", "plan_id": "nope"})
    assert r.status_code == 404
