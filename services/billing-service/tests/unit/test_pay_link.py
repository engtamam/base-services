"""BillingService.create_invoice_pay_link + resolve_pay_link — a trackable hosted link for an invoice.

The amount is the INVOICE's total (server-authoritative, never a caller field); a pending payment is
tied to the invoice so the existing webhook/reconcile seam settles it; the redirector token is
unguessable and expiring; another tenant's invoice is invisible (IDOR).
"""
from __future__ import annotations

import asyncio

import pytest

from app.adapters.datastore.memory import MemoryDataStore
from app.core.billing_service import BillingService
from app.core.models import Invoice
from app.core.stores import (CouponStore, IdempotencyStore, InvoiceStore, LedgerStore, PayLinkStore,
                             PaymentStore, PlanStore, SettlementStore, SubscriptionStore, UsageStore)
from app.ports.payment_gateway import CheckoutSession


def run(c):
    return asyncio.run(c)


class _Clock:
    def __init__(self):
        self.t = 1000.0

    def __call__(self):
        return self.t


class _Gateway:
    driver = "fake"

    def prepare_checkout(self, *, amount_minor, currency, reference, return_url="", metadata=None):
        # Echo the amount so a test can prove the invoice total (not a caller value) was used.
        return CheckoutSession(ok=True, provider="fake", id=f"sess_{amount_minor}",
                               redirect_url=f"https://checkout.fake/pay/{amount_minor}")


def _svc(st, clock):
    return BillingService(
        plans=PlanStore(st), coupons=CouponStore(st), subs=SubscriptionStore(st),
        invoices=InvoiceStore(st), payments=PaymentStore(st), usage=UsageStore(st),
        ledger=LedgerStore(st), idempo=IdempotencyStore(st), gateway=_Gateway(), tax=None,
        einvoicer=None, pdf=None, settlements=SettlementStore(st), paylinks=PayLinkStore(st),
        clock=clock,
        config={"currency": "SAR", "trial_days": 0, "vat_rate": "0.15", "reverse_charge": False,
                "grandfather": True, "dunning_base_hours": 24, "dunning_max_attempts": 4,
                "grace_days": 7, "list_limit": 100, "limits_cache_ttl": 300,
                "billable_statuses": ("active",), "max_subscriptions": 200, "merge_strategies": {},
                "rank_orders": {}, "period_tolerance_seconds": 86400, "pay_link_ttl_seconds": 3600})


async def _seed_invoice(st, tenant="acme", total=57500, status="open"):
    inv = Invoice(id="inv_1", tenant=tenant, number="INV-000001", total_minor=total,
                  subtotal_minor=total, currency="SAR", status=status)
    await InvoiceStore(st).save(inv)
    return inv


def test_pay_link_uses_the_invoice_total_and_ties_a_pending_payment():
    st = MemoryDataStore(); svc = _svc(st, _Clock())
    run(_seed_invoice(st, total=57500))
    res = run(svc.create_invoice_pay_link(tenant="acme", invoice_id="inv_1"))
    assert res["amount_minor"] == 57500                      # the INVOICE total, not a caller field
    assert res["checkout_url"] == "https://checkout.fake/pay/57500"
    assert res["token"] and res["expires_at"] == 1000.0 + 3600
    pay = run(PaymentStore(st).by_provider_id("sess_57500"))
    assert pay and pay.invoice_id == "inv_1" and pay.status == "pending" and pay.amount_minor == 57500


def test_another_tenants_invoice_is_not_found_idor():
    st = MemoryDataStore(); svc = _svc(st, _Clock())
    run(_seed_invoice(st, tenant="acme"))
    with pytest.raises(ValueError, match="INVOICE_NOT_FOUND"):
        run(svc.create_invoice_pay_link(tenant="attacker", invoice_id="inv_1"))


def test_a_paid_invoice_is_not_payable():
    st = MemoryDataStore(); svc = _svc(st, _Clock())
    run(_seed_invoice(st, status="paid"))
    with pytest.raises(ValueError, match="INVOICE_NOT_PAYABLE"):
        run(svc.create_invoice_pay_link(tenant="acme", invoice_id="inv_1"))


def test_redirector_resolves_then_expires():
    st = MemoryDataStore(); clock = _Clock(); svc = _svc(st, clock)
    run(_seed_invoice(st))
    res = run(svc.create_invoice_pay_link(tenant="acme", invoice_id="inv_1"))
    token = res["token"]
    got = run(svc.resolve_pay_link(token))
    assert got["checkout_url"] == "https://checkout.fake/pay/57500"
    assert run(svc.resolve_pay_link("nope")) is None        # unknown token
    clock.t += 3601                                          # past TTL
    assert run(svc.resolve_pay_link(token)) == {"expired": True}
