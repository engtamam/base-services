"""TuwaiqPay adapter fixes: currency normalized to ISO in fetch_status (M3), PENDING_SETTLEMENT is
not paid (L5), provider_ref is charset-validated + quoted before URL interpolation (L4), and the
amount payload respects the currency exponent (L6). No network — the http helpers are monkeypatched."""
from __future__ import annotations

import pytest

import app.adapters.payment.tuwaiqpay as tp
from app.ports.payment_gateway import GatewayUnavailable


def _gw():
    g = tp.TuwaiqPayGateway(api_key="+966500000000", extra_secret="pw")
    g._token, g._token_exp = "tok", float("inf")     # skip the auth round-trip
    return g


def _status_resp(status, *, currency, amount=150.00):
    return 200, {"data": {"transactionStatus": status, "transactionId": "abc123",
                          "bill": {"amount": amount, "currency": currency}}}


def test_fetch_status_normalizes_numeric_currency(monkeypatch):
    # a raw numeric "1" must reverse-map to SAR, else every real SAR payment flags as a mismatch
    monkeypatch.setattr(tp, "get_json", lambda *a, **k: _status_resp("SETTLED", currency="1"))
    out = _gw().fetch_status(provider_ref="abc123", currency="SAR")
    assert out.currency == "SAR" and out.amount_minor == 15000 and out.status == "succeeded"


def test_fetch_status_normalizes_object_currency(monkeypatch):
    monkeypatch.setattr(tp, "get_json",
                        lambda *a, **k: _status_resp("APPROVED", currency={"nameEn": "SAR"}))
    out = _gw().fetch_status(provider_ref="abc123", currency="SAR")
    assert out.currency == "SAR"


def test_pending_settlement_maps_to_pending(monkeypatch):
    # L5: settlement not yet captured -> pending, so we do NOT complete before capture
    monkeypatch.setattr(tp, "get_json",
                        lambda *a, **k: _status_resp("PENDING_SETTLEMENT", currency="SAR"))
    assert _gw().fetch_status(provider_ref="abc123", currency="SAR").status == "pending"


def test_malformed_provider_ref_is_rejected_before_any_call(monkeypatch):
    called = {"n": 0}

    def boom(*a, **k):
        called["n"] += 1
        return 200, {}

    monkeypatch.setattr(tp, "get_json", boom)
    with pytest.raises(GatewayUnavailable):
        _gw().fetch_status(provider_ref="../../secrets")
    assert called["n"] == 0                           # never reached the network


def test_prepare_checkout_amount_uses_currency_exponent(monkeypatch):
    captured = {}

    def fake_post(url, payload, headers, timeout=30):
        captured["p"] = payload
        return 200, {"data": {"link": "https://pay.example/x", "transactionId": "t"}}

    monkeypatch.setattr(tp, "post_json", fake_post)
    sess = _gw().prepare_checkout(
        amount_minor=1234, currency="KWD", reference="r",
        metadata={"customerName": "A", "customerMobilePhone": "+9665"})
    assert sess.ok and captured["p"]["amount"] == 1.234   # exp-3, not 12.34 from a /100
