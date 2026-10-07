"""Phase 2 — the Moyasar marketplace adapter (second provider), no network.

Proves provider-neutrality: Moyasar implements the SAME MarketplaceGateway contract as Tap, with its own
shapes (Basic auth, MINOR-unit amounts, `splits` array, secret_token webhook). Adversarial: minor-unit
splits (no conversion), verify-by-fetch anchor, a wrong secret_token refused without a fetch.
"""
from __future__ import annotations

import json

import pytest

import app.adapters.payment.moyasar as moy
from app.adapters.payment.tap import TapGateway
from app.ports.payment_gateway import (CAP_ONBOARDING, CAP_PAYOUT, CAP_SPLIT, GatewayUnavailable,
                                       MODEL_MARKETPLACE, MarketplaceGateway, ONBOARD_API,
                                       SplitDestination)


def _gw(**kw):
    return moy.MoyasarGateway(api_key="sk_test", **kw)


def test_declares_marketplace_capabilities_api_onboarding():
    g = _gw()
    assert g.model == MODEL_MARKETPLACE
    assert {CAP_SPLIT, CAP_ONBOARDING, CAP_PAYOUT} <= g.capabilities
    assert g.onboarding_modes == {ONBOARD_API}


def test_requires_secret_key():
    with pytest.raises(GatewayUnavailable):
        moy.MoyasarGateway(api_key="")


def test_both_providers_satisfy_the_marketplace_protocol():
    # provider-neutrality: the core depends on the Protocol, and BOTH adapters conform structurally.
    assert isinstance(_gw(), MarketplaceGateway)
    assert isinstance(TapGateway(api_key="sk"), MarketplaceGateway)


def test_split_checkout_uses_minor_units_no_conversion(monkeypatch):
    captured = {}

    def fake_post(url, payload, headers, timeout=None):
        captured["payload"] = payload
        captured["url"] = url
        return 200, {"id": "inv_1", "url": "https://moyasar/x", "status": "initiated"}

    monkeypatch.setattr(moy, "post_json", fake_post)
    out = _gw().prepare_split_checkout(amount_minor=10000, currency="SAR", reference="o1",
                                       destinations=[SplitDestination("rec_1", 9000)])
    assert out.ok and out.provider_ref == "inv_1" and out.redirect_url == "https://moyasar/x"
    # minor units pass straight through (Moyasar is halalas) — NOT divided to major.
    assert captured["payload"]["amount"] == 10000
    assert captured["payload"]["splits"] == [
        {"amount": 9000, "recipient_id": "rec_1", "recipient_type": "Entity"}]
    assert captured["url"].endswith("/invoices")


def test_fetch_status_paid(monkeypatch):
    monkeypatch.setattr(moy, "get_json",
                        lambda *a, **k: (200, {"id": "inv_1", "status": "paid", "amount": 10000,
                                               "currency": "SAR"}))
    out = _gw().fetch_status(provider_ref="inv_1", currency="SAR")
    assert out.status == "succeeded" and out.amount_minor == 10000


def test_fetch_status_failed(monkeypatch):
    monkeypatch.setattr(moy, "get_json",
                        lambda *a, **k: (200, {"id": "inv_1", "status": "failed", "amount": 1, "currency": "SAR"}))
    assert _gw().fetch_status(provider_ref="inv_1", currency="SAR").status == "failed"


def test_malformed_id_never_calls_network(monkeypatch):
    called = {"n": 0}
    monkeypatch.setattr(moy, "get_json", lambda *a, **k: called.__setitem__("n", called["n"] + 1) or (200, {}))
    with pytest.raises(GatewayUnavailable):
        _gw().fetch_status(provider_ref="../x")
    assert called["n"] == 0


def _wh_body(**over):
    d = {"type": "payment_paid", "data": {"id": "inv_1"}}
    d.update(over)
    return json.dumps(d).encode()


def test_webhook_verifies_by_fetch_when_unsigned(monkeypatch):
    monkeypatch.setattr(moy, "get_json",
                        lambda *a, **k: (200, {"id": "inv_1", "status": "paid", "amount": 10000, "currency": "SAR"}))
    out = _gw().verify_webhook(headers={}, body=_wh_body(), stored_currency="SAR")
    assert out.verified and out.status == "succeeded"


def test_webhook_wrong_secret_token_refused_without_fetch(monkeypatch):
    called = {"n": 0}
    monkeypatch.setattr(moy, "get_json", lambda *a, **k: called.__setitem__("n", called["n"] + 1) or (200, {}))
    g = _gw(webhook_secret="whsec")
    out = g.verify_webhook(headers={}, body=_wh_body(secret_token="wrong"), stored_currency="SAR")
    assert out.verified is False and called["n"] == 0


def test_webhook_correct_secret_token_then_fetches(monkeypatch):
    # body secret_token path: the token EQUALS our secret.
    monkeypatch.setattr(moy, "get_json",
                        lambda *a, **k: (200, {"id": "inv_1", "status": "paid", "amount": 10000, "currency": "SAR"}))
    g = _gw(webhook_secret="whsec")
    out = g.verify_webhook(headers={}, body=_wh_body(secret_token="whsec"), stored_currency="SAR")
    assert out.verified and out.status == "succeeded"


def test_webhook_correct_hmac_header_then_fetches(monkeypatch):
    # X-Moyasar-Signature path: HMAC-SHA256 of the raw body keyed with our secret (hex).
    import hmac as _h
    from hashlib import sha256
    monkeypatch.setattr(moy, "get_json",
                        lambda *a, **k: (200, {"id": "inv_1", "status": "paid", "amount": 10000, "currency": "SAR"}))
    g = _gw(webhook_secret="whsec")
    body = _wh_body()
    good = _h.new(b"whsec", body, sha256).hexdigest()
    out = g.verify_webhook(headers={"x-moyasar-signature": good}, body=body, stored_currency="SAR")
    assert out.verified and out.status == "succeeded"


def test_webhook_wrong_hmac_header_refused_without_fetch(monkeypatch):
    called = {"n": 0}
    monkeypatch.setattr(moy, "get_json", lambda *a, **k: called.__setitem__("n", called["n"] + 1) or (200, {}))
    g = _gw(webhook_secret="whsec")
    out = g.verify_webhook(headers={"x-moyasar-signature": "deadbeef"}, body=_wh_body(), stored_currency="SAR")
    assert out.verified is False and called["n"] == 0


def test_onboard_api_returns_recipient_id(monkeypatch):
    monkeypatch.setattr(moy, "post_json", lambda *a, **k: (200, {"id": "ent_1", "status": "active"}))
    r = _gw().onboard_merchant(mode=ONBOARD_API, data={"name": "Shop"})
    assert r.ok and r.destination_id == "ent_1" and r.status == "active"


def test_onboard_hosted_refused(monkeypatch):
    r = _gw().onboard_merchant(mode="hosted", data={})
    assert r.ok is False          # Moyasar declares api-only onboarding


def test_fetch_payout_paid(monkeypatch):
    monkeypatch.setattr(moy, "get_json",
                        lambda *a, **k: (200, {"id": "po_1", "status": "paid", "amount": 5000, "currency": "SAR"}))
    assert _gw().fetch_payout_status(payout_ref="po_1").status == "paid"
