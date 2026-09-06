"""Operator gateway activation: refuse activating an unconfigured gateway, and preview a switch.

Two behaviours the money path depends on:
  * activating PAYMENT_GATEWAY_DRIVER=X is refused unless X's own credentials are present (a real
    validation, reusing the adapter's own build check — not a UI hint);
  * the activation preview surfaces how many subscribers a switch would break, so it is a deliberate
    choice, not a silent breakage found later via failed renewals.
"""
from __future__ import annotations

import datetime as dt
import os

import jwt
import pytest
from fastapi.testclient import TestClient

SECRET = "h" * 40
ITOKEN = "i" * 40


@pytest.fixture()
def client():
    from app.config import get_settings
    saved = dict(os.environ)
    os.environ.update(ENV="test", INTERNAL_SERVICE_TOKEN=ITOKEN, TOKEN_VERIFIER_DRIVER="hs256",
                      TOKEN_HS256_SECRET=SECRET, RATELIMIT_MAX="1000",
                      SECRETS_KEK="0" * 64, PII_SALT="pepper", PAYMENT_GATEWAY_DRIVER="null")
    get_settings.cache_clear()
    from app.main import create_app
    c = TestClient(create_app(), raise_server_exceptions=False)
    yield c
    os.environ.clear(); os.environ.update(saved); get_settings.cache_clear()


def _admin(caps, tenant="t1"):
    now = dt.datetime.now(dt.timezone.utc)
    tok = jwt.encode({"sub": "op", "caps": caps, "tenant": tenant,
                      "exp": now + dt.timedelta(minutes=5), "iat": now}, SECRET, algorithm="HS256")
    return {"Authorization": "Bearer " + tok}


def _set_secret(client, name, value, tenant="t1"):
    return client.put("/api/v1/billing/operator/secrets",
                      json={"name": name, "value": value, "tenant": tenant},
                      headers=_admin(["platform.secrets"]))


def _activate(client, driver, tenant="t1"):
    return client.put("/api/v1/billing/operator/config",
                      json={"key": "PAYMENT_GATEWAY_DRIVER", "value": driver, "tenant": tenant},
                      headers=_admin(["platform.config"]))


def _preview(client, driver, tenant="t1"):
    return client.get(f"/api/v1/billing/operator/payment/activation-preview?driver={driver}&tenant={tenant}",
                      headers=_admin(["platform.config"]))


# ── activation validation ───────────────────────────────────────────────────
def test_activating_an_unconfigured_gateway_is_refused_by_name(client):
    r = _activate(client, "hyperpay")            # needs api_key + entityId, none set
    assert r.status_code == 400
    body = r.json()
    assert body["error"] == "GATEWAY_NOT_CONFIGURED" and "hyperpay" in body["reason"]


def test_activating_an_unknown_gateway_is_refused(client):
    r = _activate(client, "does-not-exist")
    assert r.status_code == 400 and r.json()["error"] == "GATEWAY_NOT_CONFIGURED"


def test_activating_a_fully_configured_gateway_succeeds(client):
    _set_secret(client, "PAYMENT_API_KEY", "sk_live_x")
    _set_secret(client, "PAYMENT_EXTRA_ID", "entity_1")     # hyperpay's entityId
    r = _activate(client, "hyperpay")
    assert r.status_code == 200 and r.json()["value"] == "hyperpay"


def test_activating_stripe_needs_only_its_api_key(client):
    r = _activate(client, "stripe")              # no key yet
    assert r.status_code == 400
    _set_secret(client, "PAYMENT_API_KEY", "sk_live_stripe")
    assert _activate(client, "stripe").status_code == 200


def test_null_deactivation_needs_no_credentials(client):
    assert _activate(client, "null").status_code == 200


# ── switch preview ──────────────────────────────────────────────────────────
def test_preview_reports_activatable_and_reason(client):
    unconf = _preview(client, "hyperpay").json()
    assert unconf["activatable"] is False and unconf["reason"]      # the adapter's own reason, verbatim
    _set_secret(client, "PAYMENT_API_KEY", "k"); _set_secret(client, "PAYMENT_EXTRA_ID", "e")
    ok = _preview(client, "hyperpay").json()
    assert ok["activatable"] is True and ok["reason"] == ""


def test_preview_counts_affected_subscribers_on_a_real_switch(client):
    import asyncio
    from app.core.models import Subscription
    from app.core.stores import SubscriptionStore
    store = SubscriptionStore(client.app.state.container.datastore)
    # two active tenant subs hold a saved token; one canceled one does not count.
    asyncio.run(store.save(Subscription(id="s1", tenant="t1", plan_id="p", status="active", payment_token="tok_a")))
    asyncio.run(store.save(Subscription(id="s2", tenant="t1", plan_id="p", status="active", payment_token="tok_b")))
    asyncio.run(store.save(Subscription(id="s3", tenant="t1", plan_id="p", status="canceled", payment_token="tok_c")))
    # current driver is null; previewing a switch to stripe (a real switch) counts the token-holders.
    body = _preview(client, "stripe").json()
    assert body["switching"] is True and body["affected_subscribers"] == 2


def test_preview_shows_zero_affected_when_not_actually_switching(client):
    # current driver is null; previewing null is not a switch, so nothing breaks.
    assert _preview(client, "null").json()["affected_subscribers"] == 0
