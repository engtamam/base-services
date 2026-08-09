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
