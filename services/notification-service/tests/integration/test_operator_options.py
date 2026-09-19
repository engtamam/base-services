"""The notification operator surface for commerce options (native pay enable/lock).

Enforcement of native_pay lives in test_native_pay_api; here we cover the operator ROUTES: the
capability gate, the effective list, and that an operator toggle flows through to enforcement.
"""
from __future__ import annotations

import datetime as dt
import os

import jwt
import pytest
from fastapi.testclient import TestClient

SECRET = "h" * 40
ITOKEN = "i" * 40


def _make_app(**over) -> TestClient:
    from app.config import get_settings

    env = {"ENV": "test", "INTERNAL_SERVICE_TOKEN": ITOKEN,
           "TOKEN_VERIFIER_DRIVER": "hs256", "TOKEN_HS256_SECRET": SECRET, "RATELIMIT_MAX": "1000",
           "PII_SALT": "s" * 32}
    env.update(over)
    os.environ.update(env)
    get_settings.cache_clear()
    from app.main import create_app
    return TestClient(create_app(), raise_server_exceptions=False)


@pytest.fixture()
def client():
    from app.config import get_settings
    saved = dict(os.environ)
    c = _make_app(WHATSAPP_NATIVE_PAY_ENABLED="true")
    yield c
    os.environ.clear(); os.environ.update(saved)
    get_settings.cache_clear()


def _op(caps=("platform.config",)):
    now = dt.datetime.now(dt.timezone.utc)
    tok = jwt.encode({"sub": "op", "caps": list(caps), "exp": now + dt.timedelta(minutes=5),
                      "iat": now}, SECRET, algorithm="HS256")
    return {"Authorization": f"Bearer {tok}"}


def _itok():
    return {"X-Internal-Token": ITOKEN}


def test_options_need_the_config_capability(client):
    assert client.get("/api/v1/notifications/operator/options").status_code == 401
    assert client.get("/api/v1/notifications/operator/options",
                      headers=_op(caps=["reports.read"])).status_code == 403


def test_list_shows_native_pay_off_by_default(client):
    got = client.get("/api/v1/notifications/operator/options?tenant=acme", headers=_op()).json()
    np = next(o for o in got["options"] if o["option"] == "native_pay")
    assert np["enabled"] is False and np["source"] == "default"


def test_enabling_native_pay_lets_the_route_through(client):
    body = {"recipient": "+966500000001", "tenant": "shopX", "reference_id": "ORD-1",
            "currency": "SAR", "order_type": "physical-goods",
            "items": [{"retailer_id": "SKU-1", "name": "Latte", "amount_minor": 1500, "quantity": 2}],
            "subtotal_minor": 3000, "tax_minor": 0,
            "payment_gateway": "razorpay", "payment_configuration": "x"}
    # off by default → 403
    r1 = client.post("/internal/whatsapp/pay/order-details", headers=_itok(), json=body)
    assert r1.status_code == 403 and r1.json()["code"] == "NATIVE_PAY_NOT_ENABLED_FOR_TENANT"
    # operator enables it for shopX
    put = client.put("/api/v1/notifications/operator/options", headers=_op(),
                     json={"option": "native_pay", "enabled": True, "tenant": "shopX"})
    assert put.status_code == 200 and put.json()["enabled"] is True
    # now the route reaches the send (console whatsapp provider in test) instead of the gate
    r2 = client.post("/internal/whatsapp/pay/order-details", headers=_itok(), json=body)
    assert r2.status_code == 200 and r2.json()["status"] == "sent"


def test_unknown_option_refused(client):
    r = client.put("/api/v1/notifications/operator/options", headers=_op(),
                   json={"option": "external_paylink", "enabled": True})  # that one is in payments-svc
    assert r.status_code == 400 and "unknown" in r.json()["reason"].lower()


def test_a_platform_lock_off_beats_a_tenant_opt_in(client):
    h = _op()
    client.put("/api/v1/notifications/operator/options", headers=h,
               json={"option": "native_pay", "enabled": True, "tenant": "shopY"})
    client.put("/api/v1/notifications/operator/options", headers=h,
               json={"option": "native_pay", "enabled": False, "locked": True})
    got = client.get("/api/v1/notifications/operator/options?tenant=shopY", headers=h).json()
    np = next(o for o in got["options"] if o["option"] == "native_pay")
    assert np["enabled"] is False and np["locked"] is True
