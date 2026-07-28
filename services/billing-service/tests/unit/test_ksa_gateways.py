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


def test_geidea_webhook_signature():
    gw = G.GeideaGateway("k", extra_secret="p", webhook_secret="whsec")
    body = json.dumps({"order": {"orderId": "o1", "status": "Success"}}).encode()
    import base64, hmac
    sig = base64.b64encode(hmac.new(b"whsec", body, hashlib.sha256).digest()).decode()
    assert gw.verify_webhook(headers={"signature": sig}, body=body, secret="whsec").verified
    assert not gw.verify_webhook(headers={"signature": "wrong"}, body=body, secret="whsec").verified


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
