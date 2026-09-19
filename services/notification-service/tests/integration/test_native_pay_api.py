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


def _enable_native_pay(client: TestClient, tenant: str) -> None:
    """Turn ON the per-shop native_pay option (the second gate, on top of the global region flag).
    Seeded directly on the datastore so these tests need no operator JWT."""
    import asyncio

    from app.core.option_gate import OptionGate
    gate = OptionGate(client.app.state.container.datastore)
    asyncio.run(gate.set(tenant=tenant, option="native_pay", enabled=True))


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
    _enable_native_pay(c, "acme")
    r = c.post("/internal/whatsapp/pay/order-details", headers=_tok(), json=_order_details_body())
    assert r.status_code == 200, r.text
    assert r.json()["status"] == "sent" and r.json()["provider"] == "console"
    rows = c.get("/internal/whatsapp/deliveries?tenant=acme", headers=_tok()).json()["deliveries"]
    assert any(d["template_key"] == "interactive:order_details" and d["status"] == "sent"
               for d in rows)


def test_order_details_with_a_wrong_subtotal_is_a_clear_422(saved_env):
    c = _make_app(WHATSAPP_NATIVE_PAY_ENABLED="true", WHATSAPP_PAYMENTS_DRIVER="console")
    _enable_native_pay(c, "acme")
    # items sum to 3000 but subtotal claims 9999 — refused at build time, not by Meta
    r = c.post("/internal/whatsapp/pay/order-details", headers=_tok(),
               json=_order_details_body(subtotal_minor=9999))
    assert r.status_code == 422 and "subtotal" in r.json()["reason"].lower()


def test_order_details_idempotency_key_dedupes_a_resend(saved_env):
    c = _make_app(WHATSAPP_NATIVE_PAY_ENABLED="true", WHATSAPP_PAYMENTS_DRIVER="console")
    _enable_native_pay(c, "acme")
    body = _order_details_body(idempotency_key="ord-1-send")
    first = c.post("/internal/whatsapp/pay/order-details", headers=_tok(), json=body).json()
    second = c.post("/internal/whatsapp/pay/order-details", headers=_tok(), json=body).json()
    assert second.get("duplicate") is True and second["delivery_id"] == first["delivery_id"]


# ── order_status ────────────────────────────────────────────────────────────────
def test_order_status_send_is_recorded_as_a_tenant_scoped_delivery(saved_env):
    c = _make_app(WHATSAPP_NATIVE_PAY_ENABLED="true", WHATSAPP_PAYMENTS_DRIVER="console")
    _enable_native_pay(c, "acme")
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
    _enable_native_pay(c, "acme")
    r = c.post("/internal/whatsapp/pay/order-status", headers=_tok(),
               json={"recipient": "+966", "tenant": "acme", "reference_id": "ORD-1",
                     "status": "teleported"})
    assert r.status_code == 422 and "status" in r.json()["reason"].lower()


# ── lookup: the source of truth (console driver answers deterministically) ──────
def test_lookup_returns_a_verified_status_via_the_console_driver(saved_env):
    c = _make_app(WHATSAPP_NATIVE_PAY_ENABLED="true", WHATSAPP_PAYMENTS_DRIVER="console")
    _enable_native_pay(c, "acme")
    r = c.post("/internal/whatsapp/pay/lookup", headers=_tok(),
               json={"tenant": "acme", "reference_id": "ORD-1", "payment_configuration": "acme_rzp"})
    assert r.status_code == 200, r.text
    j = r.json()
    assert j["ok"] is True and j["verified"] is True
    assert j["reference_id"] == "ORD-1" and j["transaction_id"] == "console_tx_ORD-1"


# ── refund ──────────────────────────────────────────────────────────────────────
def test_refund_via_the_console_driver_succeeds(saved_env):
    c = _make_app(WHATSAPP_NATIVE_PAY_ENABLED="true", WHATSAPP_PAYMENTS_DRIVER="console")
    _enable_native_pay(c, "acme")
    r = c.post("/internal/whatsapp/pay/refund", headers=_tok(),
               json={"tenant": "acme", "reference_id": "ORD-1", "payment_configuration": "acme_rzp",
                     "amount_minor": 3450, "currency": "SAR"})
    assert r.status_code == 200, r.text
    j = r.json()
    assert j["ok"] is True and j["refund_id"] == "console_refund_ORD-1"


# ── the per-shop second gate (native_pay option) ────────────────────────────────
def test_globally_on_but_shop_not_enabled_is_403(saved_env):
    """The region flag is on, but a shop with native_pay OFF (the default) is still refused — the
    per-tenant gate is the second lock."""
    c = _make_app(WHATSAPP_NATIVE_PAY_ENABLED="true", WHATSAPP_PAYMENTS_DRIVER="console")
    r = c.post("/internal/whatsapp/pay/order-details", headers=_tok(),
               json=_order_details_body(tenant="notenabled"))
    assert r.status_code == 403 and r.json()["code"] == "NATIVE_PAY_NOT_ENABLED_FOR_TENANT"


def test_a_locked_off_shop_stays_off_and_lookup_is_gated_too(saved_env):
    c = _make_app(WHATSAPP_NATIVE_PAY_ENABLED="true", WHATSAPP_PAYMENTS_DRIVER="console")
    # even lookup (the money-read half) is refused for a shop that is not enabled
    r = c.post("/internal/whatsapp/pay/lookup", headers=_tok(),
               json={"tenant": "notenabled", "reference_id": "ORD-9",
                     "payment_configuration": "x"})
    assert r.status_code == 403 and r.json()["code"] == "NATIVE_PAY_NOT_ENABLED_FOR_TENANT"


# ── refund is FAIL-CLOSED against the lookup (source of truth) ───────────────────
def _patch_adapter(monkeypatch, adapter):
    """Inject a payments adapter with a chosen captured state for the refund guard tests."""
    import app.api.internal_native_pay as mod

    async def _fake(request, settings, tenant):
        return adapter

    monkeypatch.setattr(mod, "_payments_adapter", _fake)


def _refund_body(**over) -> dict:
    b = {"tenant": "acme", "reference_id": "ORD-1", "payment_configuration": "acme_rzp",
         "amount_minor": 3450, "currency": "SAR"}
    b.update(over)
    return b


def test_refund_exceeding_the_captured_amount_is_refused(saved_env, monkeypatch):
    from app.adapters.whatsapp_payments.console import ConsoleWhatsAppPayments
    c = _make_app(WHATSAPP_NATIVE_PAY_ENABLED="true", WHATSAPP_PAYMENTS_DRIVER="console")
    _enable_native_pay(c, "acme")
    _patch_adapter(monkeypatch, ConsoleWhatsAppPayments(status="captured", amount_minor=1000,
                                                        currency="SAR"))
    r = c.post("/internal/whatsapp/pay/refund", headers=_tok(),
               json=_refund_body(amount_minor=2000))
    assert r.status_code == 422 and r.json()["code"] == "REFUND_EXCEEDS_CAPTURED"


def test_refund_in_the_wrong_currency_is_refused(saved_env, monkeypatch):
    from app.adapters.whatsapp_payments.console import ConsoleWhatsAppPayments
    c = _make_app(WHATSAPP_NATIVE_PAY_ENABLED="true", WHATSAPP_PAYMENTS_DRIVER="console")
    _enable_native_pay(c, "acme")
    _patch_adapter(monkeypatch, ConsoleWhatsAppPayments(status="captured", amount_minor=100000,
                                                        currency="USD"))
    r = c.post("/internal/whatsapp/pay/refund", headers=_tok(), json=_refund_body(currency="SAR"))
    assert r.status_code == 422 and r.json()["code"] == "REFUND_CURRENCY_MISMATCH"


def test_refund_of_an_already_refunded_payment_is_refused(saved_env, monkeypatch):
    """A payment that is no longer 'captured' (already fully/partially refunded) is not refundable —
    so a redelivered refund request is a no-op refusal, never a second money movement."""
    from app.adapters.whatsapp_payments.console import ConsoleWhatsAppPayments
    c = _make_app(WHATSAPP_NATIVE_PAY_ENABLED="true", WHATSAPP_PAYMENTS_DRIVER="console")
    _enable_native_pay(c, "acme")
    _patch_adapter(monkeypatch, ConsoleWhatsAppPayments(status="refunded", amount_minor=100000,
                                                        currency="SAR"))
    r = c.post("/internal/whatsapp/pay/refund", headers=_tok(), json=_refund_body())
    assert r.status_code == 409 and r.json()["code"] == "PAYMENT_NOT_REFUNDABLE"


def test_refund_when_the_lookup_cannot_verify_is_refused(saved_env, monkeypatch):
    from app.ports.whatsapp_payments import PaymentLookup, RefundResult
    c = _make_app(WHATSAPP_NATIVE_PAY_ENABLED="true", WHATSAPP_PAYMENTS_DRIVER="console")
    _enable_native_pay(c, "acme")

    class _Unverifiable:
        def get_payment_status(self, *, configuration, reference_id):
            return PaymentLookup(ok=False, reference_id=reference_id, error="graph unreachable")

        def refund(self, **_):                      # must NEVER be reached
            raise AssertionError("refund fired without a verified payment")

    _patch_adapter(monkeypatch, _Unverifiable())
    r = c.post("/internal/whatsapp/pay/refund", headers=_tok(), json=_refund_body())
    assert r.status_code == 502 and r.json()["code"] == "REFUND_LOOKUP_FAILED"


def test_refund_within_the_captured_amount_succeeds(saved_env, monkeypatch):
    from app.adapters.whatsapp_payments.console import ConsoleWhatsAppPayments
    c = _make_app(WHATSAPP_NATIVE_PAY_ENABLED="true", WHATSAPP_PAYMENTS_DRIVER="console")
    _enable_native_pay(c, "acme")
    _patch_adapter(monkeypatch, ConsoleWhatsAppPayments(status="captured", amount_minor=5000,
                                                        currency="SAR"))
    r = c.post("/internal/whatsapp/pay/refund", headers=_tok(), json=_refund_body(amount_minor=3450))
    assert r.status_code == 200, r.text
    assert r.json()["ok"] is True
