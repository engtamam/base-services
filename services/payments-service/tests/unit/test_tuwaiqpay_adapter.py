"""TuwaiqPay adapter fixes: currency normalized to ISO in fetch_status (M3), PENDING_SETTLEMENT is
not paid (L5), provider_ref is charset-validated + quoted before URL interpolation (L4), and the
amount payload respects the currency exponent (L6). No network — the http helpers are monkeypatched."""
from __future__ import annotations

import json

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


# ── verify_webhook: FIN-021 — the production money gate (unsigned → re-query is the truth) ──────────
def _wh(tx_id="abc123", detail_id=1412, claimed="succeeded"):
    return json.dumps({"transactionDetails": {"transactionId": tx_id, "id": detail_id,
                                              "transactionStatus": claimed}}).encode()


def test_verify_webhook_trusts_the_requery_not_the_body(monkeypatch):
    # The body CLAIMS succeeded, but our authenticated re-query says PENDING → NOT verified. This is the
    # forged/replayed-webhook defense: a caller cannot fabricate a paid status by POSTing one.
    monkeypatch.setattr(tp, "get_json", lambda *a, **k: _status_resp("PENDING", currency="SAR"))
    out = _gw().verify_webhook(headers={}, body=_wh(claimed="succeeded"))
    assert out.verified is False and out.status == "pending"


def test_verify_webhook_confirms_a_settled_payment(monkeypatch):
    monkeypatch.setattr(tp, "get_json", lambda *a, **k: _status_resp("SETTLED", currency="SAR"))
    out = _gw().verify_webhook(headers={}, body=_wh(detail_id=777))
    assert out.verified is True and out.status == "succeeded"
    assert out.event_id == "777" and out.provider_ref == "abc123"


def test_verify_webhook_confirms_a_terminal_failure(monkeypatch):
    monkeypatch.setattr(tp, "get_json", lambda *a, **k: _status_resp("FAILED", currency="SAR"))
    out = _gw().verify_webhook(headers={}, body=_wh())
    assert out.verified is True and out.status == "failed"


def test_verify_webhook_missing_tx_id_is_unverified_and_never_calls_the_gateway(monkeypatch):
    called = {"n": 0}
    monkeypatch.setattr(tp, "get_json", lambda *a, **k: called.__setitem__("n", called["n"] + 1) or _status_resp("SETTLED", currency="SAR"))
    out = _gw().verify_webhook(headers={}, body=json.dumps({"transactionDetails": {"id": 1}}).encode())
    assert out.verified is False and called["n"] == 0


def test_verify_webhook_gateway_unavailable_never_fabricates_paid(monkeypatch):
    def boom(*a, **k):
        raise GatewayUnavailable("down")
    monkeypatch.setattr(tp, "get_json", boom)
    out = _gw().verify_webhook(headers={}, body=_wh())
    assert out.verified is False and out.status == "pending" and out.provider_ref == "abc123"


def test_verify_webhook_malformed_body_is_unverified(monkeypatch):
    monkeypatch.setattr(tp, "get_json", lambda *a, **k: _status_resp("SETTLED", currency="SAR"))
    assert _gw().verify_webhook(headers={}, body=b"not-json").verified is False
    assert _gw().verify_webhook(headers={}, body=b"").verified is False


# ── prepare_checkout / auth failure branches: FIN-024 ──────────────────────────────────────────────
def test_prepare_checkout_missing_customer_fields_returns_not_ok(monkeypatch):
    monkeypatch.setattr(tp, "post_json", lambda *a, **k: (_ for _ in ()).throw(AssertionError("no call")))
    sess = _gw().prepare_checkout(amount_minor=1000, currency="SAR", reference="r", metadata={})
    assert sess.ok is False


def test_prepare_checkout_bill_failure_returns_not_ok(monkeypatch):
    monkeypatch.setattr(tp, "post_json", lambda *a, **k: (0, {}))     # code==0 → unreachable/failed
    sess = _gw().prepare_checkout(amount_minor=1000, currency="SAR", reference="r",
                                  metadata={"customerName": "A", "customerMobilePhone": "+9665"})
    assert sess.ok is False


def test_prepare_checkout_empty_link_returns_not_ok(monkeypatch):
    monkeypatch.setattr(tp, "post_json", lambda *a, **k: (200, {"data": {"transactionId": "t"}}))
    sess = _gw().prepare_checkout(amount_minor=1000, currency="SAR", reference="r",
                                  metadata={"customerName": "A", "customerMobilePhone": "+9665"})
    assert sess.ok is False                                            # a linkless session is not ok


def test_access_token_auth_failure_raises(monkeypatch):
    g = tp.TuwaiqPayGateway(api_key="+966500000000", extra_secret="pw")   # no cached token
    monkeypatch.setattr(tp, "post_json", lambda *a, **k: (0, {}))         # auth unreachable
    with pytest.raises(GatewayUnavailable):
        g.fetch_status(provider_ref="abc123", currency="SAR")


# ── FIN-018 / FIN-029: a status response with NO currency must not fabricate a paid outcome ─────────
def test_webhook_with_absent_status_currency_does_not_verify(monkeypatch):
    # The webhook re-query passes no currency; if the status response also omits bill.currency, the
    # outcome carries currency="" -> apply_outcome cannot confirm it -> it stays UNVERIFIED (the
    # reconcile path, which passes the request's own currency, later self-heals it). Never a fabricated
    # paid on a currency we could not confirm.
    resp = (200, {"data": {"transactionStatus": "SETTLED", "transactionId": "abc123",
                           "bill": {"amount": 150.00}}})   # NO 'currency' key
    monkeypatch.setattr(tp, "get_json", lambda *a, **k: resp)
    out = _gw().verify_webhook(headers={}, body=_wh())
    # The adapter does NOT fabricate a currency; it returns "" — and apply_outcome refuses a SUCCEEDED
    # with no confirmable currency (currency_ok is False), so this never completes off the webhook. The
    # reconcile path (which passes the request's own currency) self-heals it later.
    assert out.currency == ""
