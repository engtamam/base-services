"""Geidea + APS/PayFort adapters — verified endpoints, auth, amount units, signatures."""
from __future__ import annotations

import hashlib
import json

import pytest

import app.adapters.payment.aps as A
import app.adapters.payment.geidea as G
from app.ports.payment_gateway import GatewayUnavailable


# ── Geidea ────────────────────────────────────────────────────────────────────
def test_geidea_charge_shape():
    seen = {}
    G.post_json = lambda u, b, h=None: (seen.update(u=u, b=b, h=h) or
                                        (200, {"responseCode": "000", "order": {"orderId": "ord_1"}}))
    r = G.GeideaGateway("pubkey", extra_secret="apipass").charge(
        amount_minor=5000, currency="SAR", token="sess_1", idempotency_key="k1")
    assert r.ok and r.provider_id == "ord_1"
    assert seen["u"].endswith("/pgw/api/v2/direct/pay")
    assert seen["b"]["amount"] == 50.0 and seen["b"]["sessionId"] == "sess_1"   # MAJOR units
    assert seen["h"]["Authorization"].startswith("Basic ")


def test_geidea_declines_on_non_000():
    G.post_json = lambda u, b, h=None: (200, {"responseCode": "099", "responseMessage": "declined"})
    r = G.GeideaGateway("k", extra_secret="p").charge(amount_minor=100, currency="SAR",
                                                      token="s", idempotency_key="k")
    assert r.ok is False and r.error == "declined"


def test_geidea_requires_both_secrets():
    with pytest.raises(GatewayUnavailable):
        G.GeideaGateway("pubkey")            # no api password


def _geidea_signed(pubkey, apipass, order, timestamp):
    """Build a Geidea-shaped callback with a signature over the documented field digest:
    HMAC-SHA256 (base64) keyed by the API password over
    {pubkey}{amount:.2f}{currency}{orderId}{status}{merchantReferenceId}{timestamp}."""
    import base64, hmac
    digest = (f"{pubkey}{float(order['amount']):.2f}{order['currency']}{order['orderId']}"
              f"{order['status']}{order['merchantReferenceId']}{timestamp}")
    sig = base64.b64encode(hmac.new(apipass.encode(), digest.encode(), hashlib.sha256).digest()).decode()
    return json.dumps({"order": order, "timestamp": timestamp, "signature": sig}).encode()


def test_geidea_webhook_verifies_the_field_digest_not_the_raw_body():
    gw = G.GeideaGateway("pubk", extra_secret="apipass")
    order = {"orderId": "o1", "amount": 50.0, "currency": "SAR", "status": "Success",
             "merchantReferenceId": "ref1"}
    body = _geidea_signed("pubk", "apipass", order, "2026-01-01T00:00:00")
    ev = gw.verify_webhook(headers={}, body=body, secret="")
    assert ev.verified is True and ev.type == "payment.succeeded" and ev.provider_id == "o1"


def test_geidea_webhook_rejects_a_tampered_amount():
    gw = G.GeideaGateway("pubk", extra_secret="apipass")
    order = {"orderId": "o1", "amount": 50.0, "currency": "SAR", "status": "Success",
             "merchantReferenceId": "ref1"}
    body = _geidea_signed("pubk", "apipass", order, "2026-01-01T00:00:00")
    import json as _j
    evt = _j.loads(body); evt["order"]["amount"] = 5000.0        # attacker inflates the amount
    assert gw.verify_webhook(headers={}, body=_j.dumps(evt).encode(), secret="").verified is False


def test_geidea_webhook_rejects_a_wrong_api_password():
    gw = G.GeideaGateway("pubk", extra_secret="apipass")
    order = {"orderId": "o1", "amount": 50.0, "currency": "SAR", "status": "Success",
             "merchantReferenceId": "ref1"}
    body = _geidea_signed("pubk", "WRONGPASS", order, "2026-01-01T00:00:00")   # forged with wrong key
    assert gw.verify_webhook(headers={}, body=body, secret="").verified is False


# ── APS / PayFort ─────────────────────────────────────────────────────────────
def test_aps_signature_algorithm():
    # phrase + sorted(name=value) concatenated + phrase, SHA-256 hex
    params = {"b": "2", "a": "1"}
    expected = hashlib.sha256(b"PHRa=1b=2PHR").hexdigest()
    assert A.sign(params, "PHR") == expected
    # 'signature' itself is excluded from the digest
    assert A.sign({**params, "signature": "zzz"}, "PHR") == expected


def test_aps_charge_shape_minor_units_and_signed():
    seen = {}
    A.post_json = lambda u, b, h=None: (seen.update(u=u, b=b) or (200, {"status": "14", "fort_id": "f1"}))
    r = A.ApsGateway("acc", extra_id="merch", extra_secret="reqphrase").charge(
        amount_minor=5000, currency="SAR", token="tok_1", idempotency_key="ref1")
    assert r.ok and r.provider_id == "f1"
    assert seen["u"].endswith("/FortAPI/paymentApi")
    assert seen["b"]["command"] == "PURCHASE" and seen["b"]["amount"] == "5000"     # MINOR units
    assert seen["b"]["merchant_identifier"] == "merch" and seen["b"]["access_code"] == "acc"
    assert seen["b"]["signature"] == A.sign({k: v for k, v in seen["b"].items() if k != "signature"},
                                            "reqphrase")


def test_aps_declines_on_other_status():
    A.post_json = lambda u, b, h=None: (200, {"status": "00", "response_message": "Invalid card"})
    r = A.ApsGateway("a", extra_id="m", extra_secret="p").charge(
        amount_minor=100, currency="SAR", token="t", idempotency_key="k")
    assert r.ok is False and r.error == "Invalid card"


def test_aps_webhook_signature_uses_response_phrase():
    gw = A.ApsGateway("a", extra_id="m", extra_secret="req", webhook_secret="resp")
    evt = {"status": "14", "fort_id": "f1"}
    evt["signature"] = A.sign(evt, "resp")
    e = gw.verify_webhook(headers={}, body=json.dumps(evt).encode(), secret="resp")
    assert e.verified and e.type == "payment.succeeded" and e.provider_id == "f1"
    evt["signature"] = "tampered"
    assert not gw.verify_webhook(headers={}, body=json.dumps(evt).encode(), secret="resp").verified


def test_aps_requires_three_credentials():
    with pytest.raises(GatewayUnavailable):
        A.ApsGateway("acc", extra_id="merch")        # missing SHA request phrase


def test_both_registered():
    from app.container import _PAYMENT
    assert {"geidea", "aps", "recording"} <= set(_PAYMENT)


# ── HyperPay webhook: AES-256-GCM decrypt-and-verify (the tag IS the auth) ────────
import app.adapters.payment.hyperpay as HP  # noqa: E402


def _hp_webhook(payload: dict, key_hex: str, *, iv: bytes = b"\x00" * 16):
    """Build a real HyperPay-shaped encrypted webhook: hex body + IV/tag headers."""
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM
    plaintext = json.dumps(payload).encode("utf-8")
    out = AESGCM(bytes.fromhex(key_hex)).encrypt(iv, plaintext, None)
    ciphertext, tag = out[:-16], out[-16:]
    headers = {"X-Initialization-Vector": iv.hex(), "X-Authentication-Tag": tag.hex()}
    return headers, ciphertext.hex().encode("ascii")


_KEY = "00112233445566778899aabbccddeeff00112233445566778899aabbccddeeff"  # 32 bytes = AES-256
_PAYLOAD = {"type": "PAYMENT",
            "payload": {"id": "8ac7a4a1pay01", "paymentType": "DB",
                        "merchantTransactionId": "inv_42",
                        "result": {"code": "000.100.110", "description": "ok"}}}


def test_hyperpay_webhook_verifies_and_maps_success():
    gw = HP.HyperPayGateway("apikey", extra_id="ent1", webhook_secret=_KEY)
    headers, body = _hp_webhook(_PAYLOAD, _KEY)
    ev = gw.verify_webhook(headers=headers, body=body, secret=_KEY)
    assert ev.verified is True and ev.type == "payment.succeeded"
    assert ev.provider_id == "8ac7a4a1pay01"
    assert ev.raw["merchant_transaction_id"] == "inv_42"


def test_hyperpay_webhook_maps_decline_and_refund():
    gw = HP.HyperPayGateway("apikey", extra_id="ent1", webhook_secret=_KEY)
    decl = {"type": "PAYMENT", "payload": {"id": "p", "result": {"code": "800.100.100"}}}
    h, b = _hp_webhook(decl, _KEY)
    assert gw.verify_webhook(headers=h, body=b, secret=_KEY).type == "payment.failed"
    rf = {"type": "PAYMENT", "payload": {"id": "p", "paymentType": "RF",
                                         "result": {"code": "000.000.000"}}}
    h, b = _hp_webhook(rf, _KEY)
    assert gw.verify_webhook(headers=h, body=b, secret=_KEY).type == "refund"


def test_hyperpay_webhook_refuses_a_tampered_tag():
    """The security property: a flipped auth tag fails GCM verification — verified=False, no crash."""
    gw = HP.HyperPayGateway("apikey", extra_id="ent1", webhook_secret=_KEY)
    headers, body = _hp_webhook(_PAYLOAD, _KEY)
    bad = bytearray(bytes.fromhex(headers["X-Authentication-Tag"])); bad[0] ^= 0x01
    headers["X-Authentication-Tag"] = bad.hex()
    ev = gw.verify_webhook(headers=headers, body=body, secret=_KEY)
    assert ev.verified is False


def test_hyperpay_webhook_refuses_a_tampered_body():
    gw = HP.HyperPayGateway("apikey", extra_id="ent1", webhook_secret=_KEY)
    headers, body = _hp_webhook(_PAYLOAD, _KEY)
    ba = bytearray(bytes.fromhex(body.decode())); ba[0] ^= 0x01
    ev = gw.verify_webhook(headers=headers, body=ba.hex().encode(), secret=_KEY)
    assert ev.verified is False


def test_hyperpay_webhook_refuses_a_wrong_key():
    gw = HP.HyperPayGateway("apikey", extra_id="ent1", webhook_secret=_KEY)
    headers, body = _hp_webhook(_PAYLOAD, _KEY)
    other = "ff" * 32
    assert gw.verify_webhook(headers=headers, body=body, secret=other).verified is False


def test_hyperpay_webhook_no_key_or_headers_is_unverified_not_a_pass():
    gw = HP.HyperPayGateway("apikey", extra_id="ent1", webhook_secret="")
    headers, body = _hp_webhook(_PAYLOAD, _KEY)
    # no key configured
    assert gw.verify_webhook(headers=headers, body=body, secret="").verified is False
    # missing the IV/tag headers
    assert gw.verify_webhook(headers={}, body=body, secret=_KEY).verified is False


# ── refund currency precision: a 3-dp currency must not be formatted as 2-dp (10x bug) ──
def test_geidea_refund_uses_the_real_currency_exponent():
    seen = {}
    G.post_json = lambda u, b, h=None: (seen.update(b=b) or (200, {"responseCode": "000",
                                                                   "order": {"orderId": "r1"}}))
    # 1500 minor KWD = 1.500 KWD (3-dp). The SAR-default bug would send 15.00.
    G.GeideaGateway("k", extra_secret="p").refund(provider_id="p1", amount_minor=1500, currency="KWD")
    assert seen["b"]["amount"] == 1.5, "3-dp currency refunded with the wrong exponent"


def test_hyperpay_refund_uses_the_real_currency_exponent():
    seen = {}
    HP.post_form = lambda u, f, h=None: (seen.update(f=f) or (200, {"id": "r1",
                                                                    "result": {"code": "000.000.000"}}))
    HP.HyperPayGateway("k", extra_id="e").refund(provider_id="p1", amount_minor=1500, currency="KWD")
    assert seen["f"]["amount"] == "1.500", "HyperPay refund formatted a 3-dp currency as 2-dp"


# ── prepare_checkout: server-created session, never a key to the client ──────────
def test_hyperpay_prepare_checkout_returns_a_widget_no_key():
    HP.post_form = lambda u, f, h=None: (seen := {}) or (200, {"id": "chk_1",
                                                              "result": {"code": "000.200.100"}})
    s = HP.HyperPayGateway("apikey", extra_id="ent1").prepare_checkout(
        amount_minor=5000, currency="SAR", reference="inv_1")
    assert s.ok and s.provider == "hyperpay" and s.id == "chk_1"
    assert s.widget["checkout_id"] == "chk_1" and "paymentWidgets.js?checkoutId=chk_1" in s.widget["script_url"]
    # the entityId/api key never appears in what goes to the client
    assert "apikey" not in str(s.to_dict())


def test_geidea_prepare_checkout_returns_a_session():
    G.post_json = lambda u, b, h=None: (200, {"responseCode": "000",
                                             "session": {"id": "sess_9", "expiryDate": "2026-01-01"}})
    s = G.GeideaGateway("pubkey", extra_secret="apipass").prepare_checkout(
        amount_minor=5000, currency="SAR", reference="inv_1")
    assert s.ok and s.provider == "geidea" and s.id == "sess_9"
    assert s.widget["session_id"] == "sess_9" and s.expires_at == "2026-01-01"


def test_prepare_checkout_reports_a_provider_failure_not_a_fake_session():
    G.post_json = lambda u, b, h=None: (200, {"responseCode": "099", "responseMessage": "bad amount"})
    s = G.GeideaGateway("k", extra_secret="p").prepare_checkout(
        amount_minor=1, currency="SAR", reference="x")
    assert s.ok is False and "bad amount" in s.error
