"""Per-gateway adapter checks against the providers' verified APIs — request shape, amount units,
and (the security-critical part) webhook signature verification: a correct signature passes, a
tampered one fails, and a missing secret is never a silent pass.

One gateway per section; extended as each is audited.
"""
from __future__ import annotations

import json

import app.adapters.payment.moyasar as M


# ── Moyasar ───────────────────────────────────────────────────────────────────
def test_moyasar_charge_sends_minor_amount_token_source_and_idempotent_given_id():
    seen = {}
    M.post_form = lambda u, f, h=None: (seen.update(u=u, f=f, h=h) or
                                        (200, {"id": "pay_1", "status": "paid"}))
    gw = M.MoyasarGateway("sk_test", return_url="https://shop.test/return")
    r = gw.charge(amount_minor=5000, currency="SAR", token="tok_abc", idempotency_key="inv_9",
                  description="Pro", metadata={"tenant": "acme", "invoice": 9})
    assert r.ok and r.provider_id == "pay_1"
    assert seen["f"]["amount"] == 5000                      # MINOR units (halalas), integer
    assert seen["f"]["source[type]"] == "token" and seen["f"]["source[token]"] == "tok_abc"
    assert seen["f"]["callback_url"] == "https://shop.test/return"   # required for a token source
    assert seen["f"]["metadata[tenant]"] == "acme" and seen["f"]["metadata[invoice]"] == "9"  # stringified
    assert seen["h"]["Authorization"].startswith("Basic ")


def test_moyasar_given_id_is_a_stable_uuid_per_idempotency_key():
    seen = []
    M.post_form = lambda u, f, h=None: (seen.append(f) or (200, {"id": "p", "status": "paid"}))
    gw = M.MoyasarGateway("sk_test")
    gw.charge(amount_minor=100, currency="SAR", token="t", idempotency_key="same")
    gw.charge(amount_minor=100, currency="SAR", token="t", idempotency_key="same")
    gw.charge(amount_minor=100, currency="SAR", token="t", idempotency_key="other")
    import uuid
    assert seen[0]["given_id"] == seen[1]["given_id"]                 # same key ⇒ same id (no double charge)
    assert seen[0]["given_id"] != seen[2]["given_id"]                 # different key ⇒ different id
    uuid.UUID(seen[0]["given_id"])                                    # and it IS a valid UUID


def test_moyasar_declines_are_a_real_answer_not_an_exception():
    M.post_form = lambda u, f, h=None: (200, {"id": "p", "status": "failed",
                                              "source": {"message": "insufficient funds"}})
    r = M.MoyasarGateway("sk").charge(amount_minor=100, currency="SAR", token="t", idempotency_key="k")
    assert r.ok is False and r.error == "insufficient funds"


def test_moyasar_webhook_verifies_the_secret_token_in_the_body():
    gw = M.MoyasarGateway("sk", webhook_secret="whsec_configured")
    body = json.dumps({"type": "payment_paid", "secret_token": "whsec_configured",
                       "data": {"id": "pay_1", "status": "paid"}}).encode()
    ev = gw.verify_webhook(headers={}, body=body, secret="whsec_configured")
    assert ev.verified is True and ev.type == "payment.succeeded" and ev.provider_id == "pay_1"


def test_moyasar_webhook_rejects_a_wrong_secret_token():
    gw = M.MoyasarGateway("sk", webhook_secret="whsec_configured")
    body = json.dumps({"type": "payment_paid", "secret_token": "forged",
                       "data": {"id": "pay_1", "status": "paid"}}).encode()
    assert gw.verify_webhook(headers={}, body=body, secret="whsec_configured").verified is False


def test_moyasar_webhook_maps_refund_and_failure():
    gw = M.MoyasarGateway("sk", webhook_secret="s")
    rf = json.dumps({"type": "payment_refunded", "secret_token": "s",
                     "data": {"id": "p", "status": "refunded"}}).encode()
    assert gw.verify_webhook(headers={}, body=rf, secret="s").type == "refund"
    fail = json.dumps({"type": "payment_failed", "secret_token": "s",
                       "data": {"id": "p", "status": "failed"}}).encode()
    assert gw.verify_webhook(headers={}, body=fail, secret="s").type == "payment.failed"


# ── Tap ───────────────────────────────────────────────────────────────────────
import app.adapters.payment.tap as T  # noqa: E402
from app.adapters.payment._common import hmac_sha256_hex  # noqa: E402

# The exact fields from Tap's documented charge-webhook example.
_TAP_EVENT = {
    "id": "chg_TS05A4120230736x9K22710693", "amount": 1.00, "currency": "SAR",
    "status": "CAPTURED",
    "reference": {"gateway": "mada_pg70983e7a-a686-40ba-83e2-c5e9f4074fe5",
                  "payment": "4327230736106619650"},
    "transaction": {"created": "1698392202943"},
}
# The digest Tap documents for that event — the string we must reconstruct byte-for-byte.
_TAP_DIGEST = ("x_idchg_TS05A4120230736x9K22710693x_amount1.00x_currencySAR"
               "x_gateway_referencemada_pg70983e7a-a686-40ba-83e2-c5e9f4074fe5"
               "x_payment_reference4327230736106619650x_statusCAPTUREDx_created1698392202943")


def test_tap_webhook_reconstructs_the_documented_field_digest():
    # Proves our reconstruction equals Tap's documented string: sign THEIR digest, and our verify
    # (which builds its own digest from the payload) must accept it.
    gw = T.TapGateway("sk_test")
    sig = hmac_sha256_hex("sk_test", _TAP_DIGEST.encode())
    ev = gw.verify_webhook(headers={"hashstring": sig},
                           body=json.dumps(_TAP_EVENT).encode(), secret="sk_test")
    assert ev.verified is True and ev.type == "payment.succeeded"
    assert ev.provider_id == "chg_TS05A4120230736x9K22710693"


def test_tap_webhook_is_not_verified_by_a_raw_body_hmac():
    # The old bug: HMAC of the raw JSON body. That must NOT verify anymore.
    gw = T.TapGateway("sk_test")
    raw_sig = hmac_sha256_hex("sk_test", json.dumps(_TAP_EVENT).encode())
    assert gw.verify_webhook(headers={"hashstring": raw_sig},
                             body=json.dumps(_TAP_EVENT).encode(), secret="sk_test").verified is False


def test_tap_webhook_rejects_a_tampered_amount():
    gw = T.TapGateway("sk_test")
    sig = hmac_sha256_hex("sk_test", _TAP_DIGEST.encode())     # signature for amount 1.00
    tampered = {**_TAP_EVENT, "amount": 100.00}                # attacker inflates the amount
    assert gw.verify_webhook(headers={"hashstring": sig},
                             body=json.dumps(tampered).encode(), secret="sk_test").verified is False


def test_tap_charge_sends_major_amount_and_token_source():
    seen = {}
    T.post_json = lambda u, b, h=None: (seen.update(u=u, b=b) or
                                        (200, {"id": "chg_1", "status": "CAPTURED"}))
    r = T.TapGateway("sk").charge(amount_minor=5000, currency="SAR", token="tok",
                                  idempotency_key="k1")
    assert r.ok and r.provider_id == "chg_1"
    assert seen["b"]["amount"] == 50.0 and seen["b"]["source"]["id"] == "tok"   # MAJOR units
    assert seen["u"].endswith("/v2/charges")


# ── PayTabs (IPN/webhook = HMAC of the whole raw body with the server key) ───────
import app.adapters.payment.paytabs as PT  # noqa: E402


def test_paytabs_webhook_verifies_raw_body_hmac_with_the_server_key():
    gw = PT.PayTabsGateway("SERVERKEY", extra_id="profile1", webhook_secret="SERVERKEY")
    body = json.dumps({"tran_ref": "TST1", "payment_result": {"response_status": "A"}}).encode()
    sig = hmac_sha256_hex("SERVERKEY", body)
    # header accepted under either spelling / any case
    assert gw.verify_webhook(headers={"signature": sig}, body=body, secret="SERVERKEY").verified
    assert gw.verify_webhook(headers={"X-Paytabs-Signature": sig}, body=body, secret="SERVERKEY").verified
    ev = gw.verify_webhook(headers={"signature": sig}, body=body, secret="SERVERKEY")
    assert ev.type == "payment.succeeded" and ev.provider_id == "TST1"


def test_paytabs_webhook_rejects_a_tampered_body():
    gw = PT.PayTabsGateway("SERVERKEY", extra_id="p", webhook_secret="SERVERKEY")
    body = json.dumps({"tran_ref": "TST1", "payment_result": {"response_status": "A"}}).encode()
    sig = hmac_sha256_hex("SERVERKEY", body)
    tampered = json.dumps({"tran_ref": "TST1", "payment_result": {"response_status": "A"},
                           "x": 1}).encode()
    assert gw.verify_webhook(headers={"signature": sig}, body=tampered, secret="SERVERKEY").verified is False


def test_paytabs_charge_sends_major_amount_and_profile():
    seen = {}
    PT.post_json = lambda u, b, h=None: (seen.update(u=u, b=b) or
                                         (200, {"tran_ref": "TST9",
                                                "payment_result": {"response_status": "A"}}))
    r = PT.PayTabsGateway("SK", extra_id="profile1").charge(
        amount_minor=5000, currency="SAR", token="tok", idempotency_key="cart1")
    assert r.ok and r.provider_id == "TST9"
    assert seen["b"]["cart_amount"] == 50.0 and seen["b"]["profile_id"] == "profile1"   # MAJOR units
    assert seen["b"]["tran_type"] == "sale"


# ── Stripe (PaymentIntents; webhook = t=..,v1=HMAC with replay window) ───────────
import time as _time  # noqa: E402
import app.adapters.payment.stripe as S  # noqa: E402


def _stripe_sig(secret, body, t):
    return f"t={t},v1={hmac_sha256_hex(secret, f'{t}.'.encode() + body)}"


def test_stripe_webhook_verifies_a_fresh_signed_event():
    gw = S.StripeGateway("sk_test", webhook_secret="whsec")
    body = json.dumps({"type": "payment_intent.succeeded",
                       "data": {"object": {"id": "pi_1"}}}).encode()
    t = int(_time.time())
    ev = gw.verify_webhook(headers={"Stripe-Signature": _stripe_sig("whsec", body, t)},
                           body=body, secret="whsec")
    assert ev.verified is True and ev.type == "payment.succeeded" and ev.provider_id == "pi_1"


def test_stripe_webhook_rejects_a_replayed_old_event_even_with_a_valid_signature():
    gw = S.StripeGateway("sk_test", webhook_secret="whsec", webhook_tolerance=300)
    body = json.dumps({"type": "payment_intent.succeeded"}).encode()
    old = int(_time.time()) - 3600                       # a validly-signed event, one hour old
    assert gw.verify_webhook(headers={"Stripe-Signature": _stripe_sig("whsec", body, old)},
                             body=body, secret="whsec").verified is False


def test_stripe_webhook_rejects_a_bad_signature():
    gw = S.StripeGateway("sk_test", webhook_secret="whsec")
    t = int(_time.time())
    assert gw.verify_webhook(headers={"Stripe-Signature": f"t={t},v1=deadbeef"},
                             body=b"{}", secret="whsec").verified is False


def test_stripe_charge_sends_minor_amount_and_idempotency_key():
    seen = {}
    S.post_form = lambda u, f, h=None: (seen.update(u=u, f=f, h=h) or
                                        (200, {"id": "pi_9", "status": "succeeded"}))
    r = S.StripeGateway("sk").charge(amount_minor=5000, currency="SAR", token="pm_1",
                                     idempotency_key="k1")
    assert r.ok and r.provider_id == "pi_9"
    assert seen["f"]["amount"] == 5000 and seen["f"]["currency"] == "sar"   # MINOR units, lower-case
    assert seen["h"]["Idempotency-Key"] == "k1"


# ── Checkout.com (raw-body HMAC-SHA256 hex in Cko-Signature — verified correct) ──
import app.adapters.payment.checkout as CK  # noqa: E402


def test_checkout_webhook_verifies_raw_body_hmac_hex():
    gw = CK.CheckoutGateway("sk", webhook_secret="whsig")
    body = json.dumps({"type": "payment_captured", "data": {"id": "pay_1"}}).encode()
    sig = hmac_sha256_hex("whsig", body)
    # header accepted case-insensitively
    ev = gw.verify_webhook(headers={"Cko-Signature": sig}, body=body, secret="whsig")
    assert ev.verified is True and ev.type == "payment.succeeded" and ev.provider_id == "pay_1"


def test_checkout_webhook_rejects_a_tampered_body():
    gw = CK.CheckoutGateway("sk", webhook_secret="whsig")
    body = json.dumps({"type": "payment_captured", "data": {"id": "pay_1"}}).encode()
    sig = hmac_sha256_hex("whsig", body)
    tampered = json.dumps({"type": "payment_captured", "data": {"id": "pay_2"}}).encode()
    assert gw.verify_webhook(headers={"cko-signature": sig}, body=tampered, secret="whsig").verified is False


def test_checkout_charge_sends_minor_amount_and_token_source():
    seen = {}
    CK.post_json = lambda u, b, h=None: (seen.update(u=u, b=b) or
                                         (200, {"id": "pay_9", "status": "Authorized"}))
    r = CK.CheckoutGateway("sk").charge(amount_minor=5000, currency="SAR", token="tok_1",
                                        idempotency_key="ref1")
    assert r.ok and r.provider_id == "pay_9"
    assert seen["b"]["amount"] == 5000 and seen["b"]["source"]["token"] == "tok_1"   # MINOR units
    assert seen["u"].endswith("/payments")


# ── PayPal (webhook verified via PayPal's verify-webhook-signature API) ──────────
import app.adapters.payment.paypal as PP  # noqa: E402

_PP_HEADERS = {"PayPal-Transmission-Id": "tx1", "PayPal-Transmission-Time": "2026-01-01T00:00:00Z",
               "PayPal-Cert-Url": "https://api.paypal.com/cert", "PayPal-Auth-Algo": "SHA256withRSA",
               "PayPal-Transmission-Sig": "sig=="}
_PP_EVENT = {"event_type": "PAYMENT.CAPTURE.COMPLETED", "resource": {"id": "cap_1"}}


def _pp_gw(verify_status):
    """A PayPal gateway whose token + verify API are stubbed to a given verification_status."""
    gw = PP.PayPalGateway("client", extra_id="secret", webhook_secret="WH_ID")
    gw._token = lambda: "access_tok"
    PP.post_json = lambda u, b, h=None: ((200, {"verification_status": verify_status})
                                         if u.endswith("/verify-webhook-signature") else (200, {}))
    return gw


def test_paypal_webhook_is_verified_when_the_api_says_success():
    gw = _pp_gw("SUCCESS")
    ev = gw.verify_webhook(headers=_PP_HEADERS, body=json.dumps(_PP_EVENT).encode(), secret="WH_ID")
    assert ev.verified is True and ev.type == "payment.succeeded" and ev.provider_id == "cap_1"


def test_paypal_webhook_is_not_verified_when_the_api_says_failure():
    gw = _pp_gw("FAILURE")
    ev = gw.verify_webhook(headers=_PP_HEADERS, body=json.dumps(_PP_EVENT).encode(), secret="WH_ID")
    assert ev.verified is False


def test_paypal_webhook_posts_the_transmission_headers_and_webhook_id():
    gw = PP.PayPalGateway("client", extra_id="secret", webhook_secret="WH_ID")
    gw._token = lambda: "access_tok"
    seen = {}
    PP.post_json = lambda u, b, h=None: (seen.update(u=u, b=b) or (200, {"verification_status": "SUCCESS"}))
    gw.verify_webhook(headers=_PP_HEADERS, body=json.dumps(_PP_EVENT).encode(), secret="WH_ID")
    assert seen["u"].endswith("/v1/notifications/verify-webhook-signature")
    assert seen["b"]["webhook_id"] == "WH_ID" and seen["b"]["transmission_id"] == "tx1"
    assert seen["b"]["transmission_sig"] == "sig==" and seen["b"]["webhook_event"] == _PP_EVENT


def test_paypal_webhook_without_a_webhook_id_is_unverified_not_a_pass():
    gw = PP.PayPalGateway("client", extra_id="secret", webhook_secret="")
    assert gw.verify_webhook(headers=_PP_HEADERS, body=json.dumps(_PP_EVENT).encode(),
                             secret="").verified is False
