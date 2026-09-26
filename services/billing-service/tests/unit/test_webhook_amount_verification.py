"""Audit FIN-016 — a payment is settled ONLY for the right amount, verified against the GATEWAY.

A signed webhook proves the callback is genuine, not that it paid the invoice total. Billing now pulls
the authoritative amount from the gateway (fetch_status) and refuses to mark an invoice paid for an
underpayment, or for a webhook that claims success the gateway does not confirm — via BOTH the webhook
and the reconcile paths. A gateway with no status endpoint proceeds unverified (documented).
"""
from __future__ import annotations

import asyncio
import json

from app.adapters.datastore.memory import MemoryDataStore
from app.core.models import Invoice, Payment, iso
from app.core.stores import InvoiceStore, PaymentStore
from app.ports.payment_gateway import Charge, WebhookEvent

from tests.unit.test_audit_fixes import _svc, run

SECRET = "whsec"
TENANT = "t1"


class AmtGW:
    """Gateway whose fetch_status reports a CONTROLLABLE captured amount/status."""
    driver = "amt"

    def __init__(self, captured_minor, status="succeeded", with_fetch=True):
        self._captured = captured_minor
        self._status = status
        self._with_fetch = with_fetch
        if with_fetch:
            self.fetch_status = self._fetch      # only expose it when the gateway "supports" it

    def verify_webhook(self, *, headers, body, secret):
        e = json.loads(body)
        return WebhookEvent(verified=(secret == SECRET), type=e.get("type", ""),
                            provider_id=e.get("pid", ""), event_id=e.get("eid", ""))

    def _fetch(self, *, provider_id, currency=""):
        return Charge(ok=True, status=self._status, provider="amt", provider_id=provider_id,
                      amount_minor=self._captured, currency="SAR")

    def charge(self, **k):
        return Charge(ok=True, status="succeeded", provider="amt", provider_id="ch", amount_minor=0)


def _seed(st, total=10000, provider_id="ch1"):
    inv = Invoice(id="inv_1", tenant=TENANT, number="INV-1", status="open",
                  total_minor=total, subtotal_minor=total, currency="SAR", created=iso())
    run(InvoiceStore(st).save(inv))
    pay = Payment(id="pay_1", tenant=TENANT, invoice_id="inv_1", amount_minor=total,
                  currency="SAR", status="pending", provider_id=provider_id, created=iso())
    run(PaymentStore(st).save(pay))
    return inv


def _wh(pid="ch1", typ="payment.succeeded", eid="e1"):
    return json.dumps({"type": typ, "pid": pid, "eid": eid}).encode()


def _hook(b, gw):
    return run(b.handle_webhook("amt", {}, _wh(), SECRET, webhook_tenant=TENANT))


# ── webhook path ────────────────────────────────────────────────────────────────────
def test_matching_amount_marks_paid():
    st = MemoryDataStore(); b = _svc(st, AmtGW(captured_minor=10000))
    _seed(st, total=10000)
    _hook(b, b._gw)
    assert run(InvoiceStore(st).fetch("inv_1")).status == "paid"


def test_underpayment_is_refused_invoice_stays_open():
    st = MemoryDataStore(); b = _svc(st, AmtGW(captured_minor=8000))   # captured LESS than the invoice
    _seed(st, total=10000)
    res = _hook(b, b._gw)
    assert res.get("amount_mismatch") is True
    assert run(InvoiceStore(st).fetch("inv_1")).status == "open"        # NOT settled for an underpayment
    assert run(PaymentStore(st).fetch("pay_1")).status != "succeeded"


def test_webhook_claims_paid_but_gateway_does_not_confirm_is_refused():
    st = MemoryDataStore(); b = _svc(st, AmtGW(captured_minor=10000, status="pending"))
    _seed(st, total=10000)
    res = _hook(b, b._gw)
    assert res.get("amount_mismatch") is True
    assert run(InvoiceStore(st).fetch("inv_1")).status == "open"


def test_overpayment_is_also_refused():
    st = MemoryDataStore(); b = _svc(st, AmtGW(captured_minor=12000))
    _seed(st, total=10000)
    res = _hook(b, b._gw)
    assert res.get("amount_mismatch") is True
    assert run(InvoiceStore(st).fetch("inv_1")).status == "open"


def test_gateway_without_fetch_status_proceeds_unverified():
    # A gateway with no status endpoint cannot be checked — the charge proceeds (documented degrade).
    st = MemoryDataStore(); b = _svc(st, AmtGW(captured_minor=0, with_fetch=False))
    _seed(st, total=10000)
    _hook(b, b._gw)
    assert run(InvoiceStore(st).fetch("inv_1")).status == "paid"


# ── reconcile path shares the same guarantee ──────────────────────────────────────────
def test_reconcile_underpayment_is_refused():
    st = MemoryDataStore(); b = _svc(st, AmtGW(captured_minor=7000))
    _seed(st, total=10000)
    res = run(b.reconcile_payment("pay_1"))
    assert res.get("amount_mismatch") is True
    assert run(InvoiceStore(st).fetch("inv_1")).status == "open"


def test_reconcile_matching_amount_marks_paid():
    st = MemoryDataStore(); b = _svc(st, AmtGW(captured_minor=10000))
    _seed(st, total=10000)
    run(b.reconcile_payment("pay_1"))
    assert run(InvoiceStore(st).fetch("inv_1")).status == "paid"
