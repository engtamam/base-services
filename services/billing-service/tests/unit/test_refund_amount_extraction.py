"""Audit FIN-015 — every gateway adapter extracts the refunded amount from its OWN signed refund
payload into WebhookEvent.refunded_minor (minor units), so billing can apply a partial vs full refund.

Payload shapes verified 2026-09-26 against each provider's docs (see the note in each adapter). The
refunded amount is computed regardless of signature verification, so these assert the extraction +
the refund classification directly. HyperPay (encrypted body) is covered by the wiring + core tests.
"""
from __future__ import annotations

import json

import app.adapters.payment.tap as tap
import app.adapters.payment.stripe as stripe
import app.adapters.payment.moyasar as moyasar
import app.adapters.payment.checkout as checkout
import app.adapters.payment.geidea as geidea
import app.adapters.payment.paypal as paypal
import app.adapters.payment.paytabs as paytabs
import app.adapters.payment.aps as aps


def _ev(gw, body):
    return gw.verify_webhook(headers={}, body=json.dumps(body).encode(), secret="s")


def test_tap_refund_amount_major_to_minor():
    ev = _ev(tap.TapGateway("sk", webhook_secret="s"),
             {"status": "REFUNDED", "amount": 30.0, "currency": "SAR", "id": "chg_1"})
    assert ev.type == "refund" and ev.refunded_minor == 3000


def test_stripe_refund_uses_cumulative_amount_refunded_minor():
    ev = _ev(stripe.StripeGateway("sk", webhook_secret="s"),
             {"type": "charge.refunded", "id": "evt_1",
              "data": {"object": {"id": "ch_1", "amount_refunded": 3000}}})
    assert ev.type == "refund" and ev.refunded_minor == 3000


def test_moyasar_refund_uses_cumulative_refunded_minor():
    ev = _ev(moyasar.MoyasarGateway("sk", webhook_secret="s"),
             {"type": "payment_refunded", "secret_token": "s",
              "data": {"id": "p1", "status": "refunded", "refunded": 3000}})
    assert ev.type == "refund" and ev.refunded_minor == 3000


def test_checkout_refund_amount_minor():
    ev = _ev(checkout.CheckoutGateway("sk", webhook_secret="s"),
             {"type": "payment_refunded", "id": "evt_1", "data": {"id": "pay_1", "amount": 3000}})
    assert ev.type == "refund" and ev.refunded_minor == 3000


def test_geidea_refund_amount_major_to_minor():
    ev = _ev(geidea.GeideaGateway("pk", extra_secret="pass"),
             {"order": {"orderId": "o1", "status": "Success", "detailedStatus": "PartiallyRefunded",
                        "currency": "SAR", "totalRefundedAmount": 30.0}})
    assert ev.type == "refund" and ev.refunded_minor == 3000


def test_paypal_refund_amount_major_to_minor():
    ev = _ev(paypal.PayPalGateway("id", extra_id="wh", webhook_secret="s"),
             {"event_type": "PAYMENT.CAPTURE.REFUNDED",
              "resource": {"id": "r1", "amount": {"value": "30.00", "currency_code": "SAR"}}})
    assert ev.type == "refund" and ev.refunded_minor == 3000


def test_paytabs_refund_amount_major_to_minor():
    ev = _ev(paytabs.PayTabsGateway("prof", extra_id="profile_1", webhook_secret="s"),
             {"tran_type": "Refund", "tran_ref": "t1", "tran_total": "30.00", "cart_currency": "SAR",
              "payment_result": {"response_status": "A"}})
    assert ev.type == "refund" and ev.refunded_minor == 3000


def test_aps_refund_amount_minor():
    ev = _ev(aps.ApsGateway("ak", extra_id="mid", extra_secret="phrase"),
             {"status": "06", "amount": "3000", "fort_id": "f1"})
    assert ev.type == "refund" and ev.refunded_minor == 3000


def test_no_amount_reported_is_zero_full_refund_fallback():
    # A bare "refunded" signal with no amount → 0, which the handler treats as a full refund.
    ev = _ev(tap.TapGateway("sk", webhook_secret="s"),
             {"status": "REFUNDED", "currency": "SAR", "id": "chg_1"})
    assert ev.type == "refund" and ev.refunded_minor == 0
