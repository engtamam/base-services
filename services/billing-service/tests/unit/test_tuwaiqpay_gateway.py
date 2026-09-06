"""TuwaiqPay adapter — hosted-link checkout, status, verify-by-fetch webhook, fail-closed gaps.

Verified against the provider's Integration Docs v2.0 (docs/integrations/tuwaiqpay-docs.source.html):
auth → bill(link) → status. No token charge, no refund, no signed webhook — all handled honestly.
"""
from __future__ import annotations

import json

import pytest

import app.adapters.payment.tuwaiqpay as T
from app.ports.payment_gateway import GatewayUnavailable


def _auth_ok(resp_by_path):
    """A post_json/get_json double that routes by URL and records calls."""
    calls = []

    def fake(url, *a, **k):
        calls.append(url)
        for frag, resp in resp_by_path.items():
            if frag in url:
                return resp
        return (404, {})
    return fake, calls


def _gw():
    return T.TuwaiqPayGateway(api_key="+966543751523", extra_secret="pw",
                              base="https://uat.tuwaiqpay.test/api/v1")


_AUTH = (200, {"data": {"access_token": "tok_1", "expires_in": 3600}, "message": "SUCCESS"})


def test_requires_both_credentials():
    with pytest.raises(GatewayUnavailable):
        T.TuwaiqPayGateway(api_key="+966500000000")           # no password


def test_prepare_checkout_creates_a_bill_and_returns_the_link(monkeypatch):
    fake, calls = _auth_ok({
        "/auth/authenticate": _AUTH,
        "/integration/bills": (200, {"data": {
            "transactionId": "tx_9", "merchantTransactionId": "MTX_9", "billId": "101550",
            "link": "https://uat-payment.tuwaiqpay.test/pay/ABC", "amount": 150.00,
            "currency": "SAR", "expireDate": "2026-04-15T20:31:30", "status": "PENDING"}}),
    })
    monkeypatch.setattr(T, "post_json", fake)
    s = _gw().prepare_checkout(amount_minor=15000, currency="SAR", reference="order#1",
                               metadata={"customerName": "Ahmed", "customerMobilePhone": "+966500000001",
                                         "description": "Order #1"})
    assert s.ok and s.redirect_url == "https://uat-payment.tuwaiqpay.test/pay/ABC"
    assert s.id == "tx_9" and s.widget["merchantTransactionId"] == "MTX_9"
    # amount converted minor->float major; auth happened before the bill.
    assert calls[0].endswith("/auth/authenticate") and calls[1].endswith("/integration/bills")


def test_prepare_checkout_requires_customer_name_and_mobile(monkeypatch):
    monkeypatch.setattr(T, "post_json", _auth_ok({"/auth/authenticate": _AUTH})[0])
    s = _gw().prepare_checkout(amount_minor=15000, currency="SAR", reference="r", metadata={})
    assert s.ok is False and "customerName" in s.error


def test_amount_and_currency_are_mapped_correctly(monkeypatch):
    captured = {}

    def fake(url, payload=None, headers=None, *a, **k):
        if "/auth" in url:
            return _AUTH
        captured.update(payload or {})
        return (200, {"data": {"link": "https://x/pay/1", "transactionId": "t"}})
    monkeypatch.setattr(T, "post_json", fake)
    _gw().prepare_checkout(amount_minor=15050, currency="SAR", reference="r",
                           metadata={"customerName": "A", "customerMobilePhone": "+9665"})
    assert captured["amount"] == 150.50 and captured["currencyId"] == 1   # halalas->SAR float, SAR=1


def test_token_is_cached_across_calls(monkeypatch):
    fake, calls = _auth_ok({"/auth/authenticate": _AUTH,
                            "/integration/bills": (200, {"data": {"link": "https://x/1", "transactionId": "t"}})})
    monkeypatch.setattr(T, "post_json", fake)
    g = _gw()
    md = {"customerName": "A", "customerMobilePhone": "+9665"}
    g.prepare_checkout(amount_minor=100, currency="SAR", reference="r", metadata=md)
    g.prepare_checkout(amount_minor=100, currency="SAR", reference="r", metadata=md)
    assert calls.count("https://uat.tuwaiqpay.test/api/v1/auth/authenticate") == 1   # authed once


@pytest.mark.parametrize("provider_status,expected", [
    ("SETTLED", "succeeded"), ("APPROVED", "succeeded"), ("PENDING_SETTLEMENT", "succeeded"),
    ("FAILED", "failed"), ("DECLINED", "failed"), ("CANCELLED", "cancelled"), ("PENDING", "pending"),
])
def test_fetch_status_maps_provider_statuses(monkeypatch, provider_status, expected):
    monkeypatch.setattr(T, "post_json", lambda u, *a, **k: _AUTH)
    monkeypatch.setattr(T, "get_json", lambda u, *a, **k: (200, {"data": {
        "transactionId": "tx_9", "transactionStatus": provider_status,
        "bill": {"amount": 150.00, "currency": "SAR"}}}))
    c = _gw().fetch_status(provider_id="tx_9")
    assert c.status == expected and c.amount_minor == 15000 and c.provider_id == "tx_9"


def test_webhook_is_verified_by_re_querying_not_by_trusting_the_body(monkeypatch):
    monkeypatch.setattr(T, "post_json", lambda u, *a, **k: _AUTH)
    # The GET (truth) says SETTLED, regardless of what the body claims.
    monkeypatch.setattr(T, "get_json", lambda u, *a, **k: (200, {"data": {
        "transactionId": "tx_9", "transactionStatus": "SETTLED", "bill": {"amount": 150.0, "currency": "SAR"}}}))
    body = json.dumps({"transactionDetails": {"transactionId": "tx_9", "transactionStatus": "SETTLED"}}).encode()
    ev = _gw().verify_webhook(headers={}, body=body, secret="")
    assert ev.verified and ev.type == "payment.succeeded" and ev.provider_id == "tx_9"


def test_a_forged_webhook_body_is_not_trusted(monkeypatch):
    monkeypatch.setattr(T, "post_json", lambda u, *a, **k: _AUTH)
    # Attacker POSTs a "SETTLED" body, but our authenticated GET says the tx is still PENDING.
    monkeypatch.setattr(T, "get_json", lambda u, *a, **k: (200, {"data": {
        "transactionId": "tx_9", "transactionStatus": "PENDING", "bill": {}}}))
    body = json.dumps({"transactionDetails": {"transactionId": "tx_9", "transactionStatus": "SETTLED"}}).encode()
    ev = _gw().verify_webhook(headers={}, body=body, secret="")
    assert ev.verified is False        # not confirmed by the source of truth → never marked paid


def test_charge_and_refund_fail_closed():
    g = _gw()
    with pytest.raises(GatewayUnavailable):
        g.charge(amount_minor=100, currency="SAR", token="t", idempotency_key="k")
    with pytest.raises(GatewayUnavailable):
        g.refund(provider_id="tx_9")


def test_auth_failure_raises(monkeypatch):
    monkeypatch.setattr(T, "post_json", lambda u, *a, **k: (401, {"message": "Invalid credentials"}))
    with pytest.raises(GatewayUnavailable):
        _gw().fetch_status(provider_id="tx_9")
