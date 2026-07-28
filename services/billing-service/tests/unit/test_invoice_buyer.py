"""render_pdf resolves the BILL TO party from the subscription, not the raw tenant id.

The invoice stores no buyer — the buyer's legal identity (name / VAT / CR) lives on the
subscription. Before this fix the PDF fell back to printing `invoice.tenant` (a UUID) as if it were
the buyer's name. These tests pin the resolution: a named subscription prints the name; an unnamed
one prints the tenant only as an ACCOUNT reference.
"""
from __future__ import annotations

import asyncio

from app.adapters.datastore.memory import MemoryDataStore
from app.core.billing_service import BillingService
from app.core.models import Invoice, Subscription
from app.core.stores import (CouponStore, IdempotencyStore, InvoiceStore, LedgerStore,
                             PaymentStore, PlanStore, SettlementStore, SubscriptionStore, UsageStore)


def run(c):
    return asyncio.run(c)


class _CapturePdf:
    """Captures the dict the service hands the renderer."""
    seen: dict | None = None

    def render(self, invoice: dict):
        self.seen = invoice
        return ("application/pdf", b"%PDF-fake")


def _svc(pdf):
    st = MemoryDataStore()
    return BillingService(
        plans=PlanStore(st), coupons=CouponStore(st), subs=SubscriptionStore(st),
        invoices=InvoiceStore(st), payments=PaymentStore(st), usage=UsageStore(st),
        ledger=LedgerStore(st), idempo=IdempotencyStore(st), gateway=None, tax=None,
        einvoicer=None, pdf=pdf, settlements=SettlementStore(st),
        config={"currency": "SAR", "trial_days": 0, "vat_rate": "0.15", "reverse_charge": False,
                "grandfather": True, "dunning_base_hours": 24, "dunning_max_attempts": 4,
                "grace_days": 7, "list_limit": 100, "limits_cache_ttl": 300,
                "billable_statuses": ("active",), "max_subscriptions": 200,
                "merge_strategies": {}, "rank_orders": {}})


def test_named_subscription_puts_its_legal_name_in_bill_to():
    pdf = _CapturePdf()
    svc = _svc(pdf)
    run(svc._subs.save(Subscription(id="sub_1", tenant="acme", plan_id="p1",
                                    buyer_name="Acme Trading Co", buyer_vat="300987654300003")))
    inv = Invoice(id="inv_1", tenant="acme", subscription_id="sub_1", number="INV-1")
    run(svc.render_pdf(inv))
    assert pdf.seen["buyer"] == {"name": "Acme Trading Co", "vat_number": "300987654300003"}


def test_no_subscription_shows_the_tenant_as_account_only():
    pdf = _CapturePdf()
    svc = _svc(pdf)
    inv = Invoice(id="inv_2", tenant="3477400ca907440297631def92b27a07", number="INV-2")
    run(svc.render_pdf(inv))
    # never a bare `name` equal to the tenant id — the id is an account reference, not a name
    assert pdf.seen["buyer"] == {"account": "3477400ca907440297631def92b27a07"}


def test_unnamed_subscription_still_falls_back_to_account():
    pdf = _CapturePdf()
    svc = _svc(pdf)
    run(svc._subs.save(Subscription(id="sub_3", tenant="acme", plan_id="p1", buyer_name="")))
    inv = Invoice(id="inv_3", tenant="acme", subscription_id="sub_3", number="INV-3")
    run(svc.render_pdf(inv))
    assert pdf.seen["buyer"] == {"account": "acme"}
