"""/internal/whatsapp/pay — the native "Review and Pay" seam (build-ahead, KSA-DORMANT).

Every route is gated by WHATSAPP_NATIVE_PAY_ENABLED and answers 503 until a deployment turns it on
(native pay via a gateway is India-only today). With it on: order_details / order_status go out on the
ordinary interactive send path (recorded as a tenant-scoped Delivery), while lookup + refund are the
money-carrying server-to-server half — here exercised through the deterministic `console` driver so
the whole seam is testable without Meta or a live gateway. The lookup is the SOURCE OF TRUTH; a
type:payment webhook is only a hint.
"""
from __future__ import annotations

import os

import pytest
from fastapi.testclient import TestClient

ITOKEN = "i" * 40


def _make_app(**over) -> TestClient:
    from app.config import get_settings

    env = {"ENV": "test", "INTERNAL_SERVICE_TOKEN": ITOKEN,
           "TOKEN_VERIFIER_DRIVER": "hs256", "TOKEN_HS256_SECRET": "h" * 40, "RATELIMIT_MAX": "1000"}
    env.update(over)
    os.environ.update(env)
    get_settings.cache_clear()
    from app.main import create_app
    return TestClient(create_app(), raise_server_exceptions=False)


@pytest.fixture()
def saved_env():
    from app.config import get_settings
    saved = dict(os.environ)
    yield
    os.environ.clear(); os.environ.update(saved)
    get_settings.cache_clear()


def _tok():
    return {"X-Internal-Token": ITOKEN}


def _order_details_body(**over) -> dict:
    body = {"recipient": "+966500000001", "tenant": "acme", "reference_id": "ORD-1",
            "currency": "SAR", "order_type": "physical-goods",
            "items": [{"retailer_id": "SKU-1", "name": "Latte", "amount_minor": 1500, "quantity": 2}],
            "subtotal_minor": 3000, "tax_minor": 450, "shipping_minor": 0, "discount_minor": 0,
            "payment_gateway": "razorpay", "payment_configuration": "acme_rzp",
            "body": "Review and pay"}
    body.update(over)
    return body


# ── the KSA-dormant gate: every route is 503 until enabled ──────────────────────
def test_all_routes_are_503_when_native_pay_is_disabled(saved_env):
    c = _make_app()  # WHATSAPP_NATIVE_PAY_ENABLED defaults False
    assert c.post("/internal/whatsapp/pay/order-details", headers=_tok(),
                  json=_order_details_body()).status_code == 503
    assert c.post("/internal/whatsapp/pay/order-status", headers=_tok(),
                  json={"recipient": "+966", "reference_id": "ORD-1", "status": "shipped"}
                  ).status_code == 503
    assert c.post("/internal/whatsapp/pay/lookup", headers=_tok(),
                  json={"reference_id": "ORD-1", "payment_configuration": "acme_rzp"}
                  ).status_code == 503
    assert c.post("/internal/whatsapp/pay/refund", headers=_tok(),
                  json={"reference_id": "ORD-1", "payment_configuration": "acme_rzp",
                        "amount_minor": 3450, "currency": "SAR"}).status_code == 503


def test_the_disabled_body_names_the_knob(saved_env):
    c = _make_app()
    r = c.post("/internal/whatsapp/pay/lookup", headers=_tok(),
               json={"reference_id": "ORD-1", "payment_configuration": "acme_rzp"})
    assert r.json()["code"] == "NATIVE_PAY_DISABLED"
    assert "WHATSAPP_NATIVE_PAY_ENABLED" in r.json()["reason"]


# ── the internal token is required even when enabled ────────────────────────────
def test_routes_require_the_internal_token(saved_env):
    c = _make_app(WHATSAPP_NATIVE_PAY_ENABLED="true", WHATSAPP_PAYMENTS_DRIVER="console")
    assert c.post("/internal/whatsapp/pay/order-details",
                  json=_order_details_body()).status_code == 401
    assert c.post("/internal/whatsapp/pay/lookup",
                  json={"reference_id": "ORD-1", "payment_configuration": "acme_rzp"}
                  ).status_code == 401


# ── order_details: sent through the shared interactive path, recorded as a Delivery ─
def test_order_details_send_is_recorded_as_a_tenant_scoped_delivery(saved_env):
    c = _make_app(WHATSAPP_NATIVE_PAY_ENABLED="true", WHATSAPP_PAYMENTS_DRIVER="console")
    r = c.post("/internal/whatsapp/pay/order-details", headers=_tok(), json=_order_details_body())
    assert r.status_code == 200, r.text
    assert r.json()["status"] == "sent" and r.json()["provider"] == "console"
    rows = c.get("/internal/whatsapp/deliveries?tenant=acme", headers=_tok()).json()["deliveries"]
    assert any(d["template_key"] == "interactive:order_details" and d["status"] == "sent"
               for d in rows)


def test_order_details_with_a_wrong_subtotal_is_a_clear_422(saved_env):
    c = _make_app(WHATSAPP_NATIVE_PAY_ENABLED="true", WHATSAPP_PAYMENTS_DRIVER="console")
    # items sum to 3000 but subtotal claims 9999 — refused at build time, not by Meta
    r = c.post("/internal/whatsapp/pay/order-details", headers=_tok(),
               json=_order_details_body(subtotal_minor=9999))
    assert r.status_code == 422 and "subtotal" in r.json()["reason"].lower()


def test_order_details_idempotency_key_dedupes_a_resend(saved_env):
    c = _make_app(WHATSAPP_NATIVE_PAY_ENABLED="true", WHATSAPP_PAYMENTS_DRIVER="console")
    body = _order_details_body(idempotency_key="ord-1-send")
    first = c.post("/internal/whatsapp/pay/order-details", headers=_tok(), json=body).json()
    second = c.post("/internal/whatsapp/pay/order-details", headers=_tok(), json=body).json()
    assert second.get("duplicate") is True and second["delivery_id"] == first["delivery_id"]


# ── order_status ────────────────────────────────────────────────────────────────
def test_order_status_send_is_recorded_as_a_tenant_scoped_delivery(saved_env):
    c = _make_app(WHATSAPP_NATIVE_PAY_ENABLED="true", WHATSAPP_PAYMENTS_DRIVER="console")
    r = c.post("/internal/whatsapp/pay/order-status", headers=_tok(),
               json={"recipient": "+966500000001", "tenant": "acme", "reference_id": "ORD-1",
                     "status": "shipped", "description": "On its way"})
    assert r.status_code == 200, r.text
    assert r.json()["status"] == "sent"
    rows = c.get("/internal/whatsapp/deliveries?tenant=acme", headers=_tok()).json()["deliveries"]
    assert any(d["template_key"] == "interactive:order_status" and d["status"] == "sent"
               for d in rows)


def test_order_status_with_an_unknown_status_is_a_422(saved_env):
    c = _make_app(WHATSAPP_NATIVE_PAY_ENABLED="true", WHATSAPP_PAYMENTS_DRIVER="console")
    r = c.post("/internal/whatsapp/pay/order-status", headers=_tok(),
               json={"recipient": "+966", "reference_id": "ORD-1", "status": "teleported"})
    assert r.status_code == 422 and "status" in r.json()["reason"].lower()


# ── lookup: the source of truth (console driver answers deterministically) ──────
def test_lookup_returns_a_verified_status_via_the_console_driver(saved_env):
    c = _make_app(WHATSAPP_NATIVE_PAY_ENABLED="true", WHATSAPP_PAYMENTS_DRIVER="console")
    r = c.post("/internal/whatsapp/pay/lookup", headers=_tok(),
               json={"tenant": "acme", "reference_id": "ORD-1", "payment_configuration": "acme_rzp"})
    assert r.status_code == 200, r.text
    j = r.json()
    assert j["ok"] is True and j["verified"] is True
    assert j["reference_id"] == "ORD-1" and j["transaction_id"] == "console_tx_ORD-1"


# ── refund ──────────────────────────────────────────────────────────────────────
def test_refund_via_the_console_driver_succeeds(saved_env):
    c = _make_app(WHATSAPP_NATIVE_PAY_ENABLED="true", WHATSAPP_PAYMENTS_DRIVER="console")
    r = c.post("/internal/whatsapp/pay/refund", headers=_tok(),
               json={"tenant": "acme", "reference_id": "ORD-1", "payment_configuration": "acme_rzp",
                     "amount_minor": 3450, "currency": "SAR"})
    assert r.status_code == 200, r.text
    j = r.json()
    assert j["ok"] is True and j["refund_id"] == "console_refund_ORD-1"
