"""Phase 1 — the Tap marketplace adapter, no network (http helpers monkeypatched).

Adversarial: a split charge sends major-unit amounts + destinations; a captured charge is the only
success; a malformed id never reaches the network; a webhook with a present-but-WRONG hashstring is
refused WITHOUT a fetch (no free authenticated call), and the trust anchor is always the verify-by-fetch.
"""
from __future__ import annotations

import hmac
import json
from hashlib import sha256

import pytest

import app.adapters.payment.tap as tap
from app.ports.payment_gateway import (CAP_ONBOARDING, CAP_PAYOUT, CAP_SPLIT, GatewayUnavailable,
                                       MODEL_MARKETPLACE, ONBOARD_API, ONBOARD_HOSTED, SplitDestination)


def _gw(**kw):
    return tap.TapGateway(api_key="sk_test", **kw)


# ── declaration ─────────────────────────────────────────────────────────────────
def test_declares_marketplace_capabilities():
    g = _gw()
    assert g.model == MODEL_MARKETPLACE
    assert {CAP_SPLIT, CAP_ONBOARDING, CAP_PAYOUT} <= g.capabilities
    assert g.onboarding_modes == {ONBOARD_API, ONBOARD_HOSTED}


def test_requires_a_secret_key():
    with pytest.raises(GatewayUnavailable):
        tap.TapGateway(api_key="")


# ── split checkout ────────────────────────────────────────────────────────────────
def test_prepare_split_checkout_sends_destinations_in_major_units(monkeypatch):
    captured = {}

    def fake_post(url, payload, headers, timeout=None):
        captured["url"] = url
        captured["payload"] = payload
        return 200, {"id": "chg_1", "transaction": {"url": "https://pay.tap/x"}, "status": "INITIATED"}

    monkeypatch.setattr(tap, "post_json", fake_post)
    out = _gw().prepare_split_checkout(
        amount_minor=10000, currency="SAR", reference="ord-1",
        destinations=[SplitDestination("dst_1", 9000)],
        metadata={"customerName": "Sara", "customerEmail": "s@x.com"})
    assert out.ok and out.provider_ref == "chg_1" and out.redirect_url == "https://pay.tap/x"
    # minor 10000 -> major 100.00; destination minor 9000 -> 90.00
    assert captured["payload"]["amount"] == 100.0
    assert captured["payload"]["destinations"]["destination"] == [
        {"id": "dst_1", "amount": 90.0, "currency": "SAR"}]
    assert captured["url"].endswith("/charges")


def test_split_checkout_requires_customer_identity():
    out = _gw().prepare_split_checkout(amount_minor=1000, currency="SAR", reference="o",
                                       destinations=[SplitDestination("d", 500)], metadata={})
    assert out.ok is False and "customer" in out.error.lower()


def test_split_checkout_reports_a_gateway_error(monkeypatch):
    monkeypatch.setattr(tap, "post_json", lambda *a, **k: (0, {"message": "bad"}))
    out = _gw().prepare_split_checkout(amount_minor=1000, currency="SAR", reference="o",
                                       destinations=[SplitDestination("d", 500)],
                                       metadata={"customerName": "A", "customerEmail": "a@b.c"})
    assert out.ok is False


# ── status ──────────────────────────────────────────────────────────────────────
def test_fetch_status_captured_is_success(monkeypatch):
    monkeypatch.setattr(tap, "get_json",
                        lambda *a, **k: (200, {"id": "chg_1", "status": "CAPTURED", "amount": 100.0,
                                               "currency": "SAR"}))
    out = _gw().fetch_status(provider_ref="chg_1", currency="SAR")
    assert out.status == "succeeded" and out.amount_minor == 10000 and out.verified


def test_fetch_status_initiated_is_pending(monkeypatch):
    monkeypatch.setattr(tap, "get_json",
                        lambda *a, **k: (200, {"id": "c", "status": "INITIATED", "amount": 1.0, "currency": "SAR"}))
    assert _gw().fetch_status(provider_ref="c", currency="SAR").status == "pending"


def test_fetch_status_declined_is_failed(monkeypatch):
    monkeypatch.setattr(tap, "get_json",
                        lambda *a, **k: (200, {"id": "c", "status": "DECLINED", "amount": 1.0, "currency": "SAR"}))
    assert _gw().fetch_status(provider_ref="c", currency="SAR").status == "failed"


def test_captured_with_unparseable_amount_retries(monkeypatch):
    monkeypatch.setattr(tap, "get_json",
                        lambda *a, **k: (200, {"id": "c", "status": "CAPTURED", "amount": 0, "currency": "SAR"}))
    with pytest.raises(GatewayUnavailable):
        _gw().fetch_status(provider_ref="c", currency="SAR")


def test_malformed_charge_id_never_calls_the_network(monkeypatch):
    called = {"n": 0}
    monkeypatch.setattr(tap, "get_json", lambda *a, **k: called.__setitem__("n", called["n"] + 1) or (200, {}))
    with pytest.raises(GatewayUnavailable):
        _gw().fetch_status(provider_ref="../../secrets")
    assert called["n"] == 0


# ── webhook verification (attacker view) ──────────────────────────────────────────
def _body(**over):
    d = {"id": "chg_1", "status": "CAPTURED", "amount": 100.0, "currency": "SAR"}
    d.update(over)
    return json.dumps(d).encode()


def test_webhook_verifies_by_fetch_when_unsigned(monkeypatch):
    # No webhook secret configured → trust anchor is the re-fetch.
    monkeypatch.setattr(tap, "get_json",
                        lambda *a, **k: (200, {"id": "chg_1", "status": "CAPTURED", "amount": 100.0,
                                               "currency": "SAR"}))
    out = _gw().verify_webhook(headers={}, body=_body(), stored_currency="SAR")
    assert out.verified and out.status == "succeeded"


def test_webhook_with_wrong_signature_is_refused_without_fetch(monkeypatch):
    called = {"n": 0}
    monkeypatch.setattr(tap, "get_json", lambda *a, **k: called.__setitem__("n", called["n"] + 1) or (200, {}))
    g = _gw(webhook_secret="whsec")
    out = g.verify_webhook(headers={"hashstring": "deadbeef"}, body=_body(), stored_currency="SAR")
    assert out.verified is False
    assert called["n"] == 0                    # a forged signature never earns an authenticated fetch


def test_webhook_with_correct_signature_then_fetches(monkeypatch):
    g = _gw(webhook_secret="whsec")
    body = _body()
    good = g._hashstring(json.loads(body))     # the signature Tap would send
    monkeypatch.setattr(tap, "get_json",
                        lambda *a, **k: (200, {"id": "chg_1", "status": "CAPTURED", "amount": 100.0,
                                               "currency": "SAR"}))
    out = g.verify_webhook(headers={"hashstring": good}, body=body, stored_currency="SAR")
    assert out.verified and out.status == "succeeded"


def test_webhook_body_without_id_is_unverified():
    out = _gw().verify_webhook(headers={}, body=json.dumps({"status": "CAPTURED"}).encode())
    assert out.verified is False


def test_webhook_unreachable_gateway_propagates(monkeypatch):
    # verify-by-fetch unreachable must raise (route 503s + Tap retries), never a silent verified=False.
    monkeypatch.setattr(tap, "get_json", lambda *a, **k: (0, {}))
    with pytest.raises(GatewayUnavailable):
        _gw().verify_webhook(headers={}, body=_body(), stored_currency="SAR")


# ── onboarding ────────────────────────────────────────────────────────────────────
def test_onboard_api_creates_a_lead_pending(monkeypatch):
    # api onboarding posts to the v3 lead API and returns PENDING (no destination yet) — the usable
    # destination id arrives after approval. fail-closed: not payable until active.
    captured = {}

    def fake_post(url, payload, headers, timeout=None):
        captured["url"] = url
        return 200, {"id": "led_123", "status": "created"}
    monkeypatch.setattr(tap, "post_json", fake_post)
    r = _gw().onboard_merchant(mode=ONBOARD_API, data={"brand": {"name": "Shop"}})
    assert r.ok and r.status == "pending" and r.destination_id == ""
    assert r.extra["lead_id"] == "led_123"
    assert captured["url"] == "https://api.tap.company/v3/lead"      # v3, not the v2 charge host


def test_onboard_api_failure(monkeypatch):
    monkeypatch.setattr(tap, "post_json", lambda *a, **k: (0, {"message": "nope"}))
    r = _gw().onboard_merchant(mode=ONBOARD_API, data={})
    assert r.ok is False


def test_onboard_hosted_returns_pending_handle():
    r = _gw().onboard_merchant(mode=ONBOARD_HOSTED, data={"handle": "jsc_123"})
    assert r.ok and r.status == "pending" and r.handle == "jsc_123" and r.destination_id == ""


def test_onboard_unknown_mode_refused():
    r = _gw().onboard_merchant(mode="telepathy", data={})
    assert r.ok is False


# ── payout ────────────────────────────────────────────────────────────────────────
def test_fetch_payout_paid(monkeypatch):
    # retrieve-a-payout = POST /v2/payouts/list with the id in the body (Tap has no GET /payouts/{id})
    captured = {}

    def fake_post(url, payload, headers, timeout=None):
        captured["url"] = url
        captured["payload"] = payload
        return 200, {"payouts": [{"id": "po_1", "status": "PAID_OUT", "amount": 50.0, "currency": "SAR"}]}
    monkeypatch.setattr(tap, "post_json", fake_post)
    p = _gw().fetch_payout_status(payout_ref="po_1")
    assert p.status == "paid" and p.amount_minor == 5000
    assert captured["url"].endswith("/payouts/list") and captured["payload"]["payout_id"] == "po_1"


def test_fetch_payout_failed(monkeypatch):
    monkeypatch.setattr(tap, "post_json",
                        lambda *a, **k: (200, {"payouts": [{"id": "po_1", "status": "FAILED"}]}))
    assert _gw().fetch_payout_status(payout_ref="po_1").status == "failed"


def test_fetch_payout_malformed_id_rejected():
    with pytest.raises(GatewayUnavailable):
        _gw().fetch_payout_status(payout_ref="../x")
