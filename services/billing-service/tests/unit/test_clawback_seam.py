"""The _claw_back seam — 51.3. One reversal, every path.

A grant must be reversed whichever way a payment comes undone: a card refund, a gateway refund
webhook, or a recalled off-gateway settlement. These tests drive BillingService through each path
and assert exactly one clawback row lands on the distinct key — and that the operator choices
(disabled, partial) are honoured.
"""
from __future__ import annotations

import asyncio

from app.adapters.datastore.memory import MemoryDataStore
from app.core.billing_service import BillingService
from app.core.grants import GrantOutbox, clawback_source_for
from app.core.models import Invoice, Payment, Settlement
from app.core.stores import (CouponStore, IdempotencyStore, InvoiceStore, LedgerStore,
                             PaymentStore, PlanStore, SettlementStore, SubscriptionStore, UsageStore)


def run(c):
    return asyncio.run(c)


class _Gateway:
    driver = "fake"

    class _R:
        ok = True
        status = "refunded"

    def refund(self, *, provider_id, amount_minor=None, currency="SAR"):
        return self._R()


def _svc(st, *, clawback_enabled=True, on_partial=False, grants=None, gw=None):
    cfg = {"currency": "SAR", "trial_days": 0, "vat_rate": "0.15", "reverse_charge": False,
           "grandfather": True, "dunning_base_hours": 24, "dunning_max_attempts": 4,
           "grace_days": 7, "list_limit": 100, "limits_cache_ttl": 300,
           "billable_statuses": ("active",), "max_subscriptions": 200, "merge_strategies": {},
           "rank_orders": {}, "grant_fund": "credits", "plan_credit_grant_mode": "accumulate",
           "clawback_enabled": clawback_enabled, "clawback_allow_negative": True,
           "clawback_on_partial": on_partial, "clawback_skip_expired": True}
    return BillingService(
        plans=PlanStore(st), coupons=CouponStore(st), subs=SubscriptionStore(st),
        invoices=InvoiceStore(st), payments=PaymentStore(st), usage=UsageStore(st),
        ledger=LedgerStore(st), idempo=IdempotencyStore(st), gateway=gw or _Gateway(), tax=None,
        einvoicer=None, pdf=None, settlements=SettlementStore(st), grants=grants, config=cfg)


def _outbox(st):
    from app.core.models import iso, now_ts
    return GrantOutbox(st, None, clock=now_ts, iso=iso,
                       cfg={"grant_fund": "credits", "grant_retry_base_seconds": 30,
                            "grant_retry_max_seconds": 3600})


def _granted_invoice(st, total=5000):
    inv = Invoice(id="inv_1", tenant="acme", number="INV-1", status="paid",
                  total_minor=total, currency="SAR", meta={"grant_minor": total})
    run(InvoiceStore(st).save(inv))
    return inv


def _has_clawback(st):
    return run(st.get("credit_grants", clawback_source_for("inv_1"))) is not None


# ── path 1: card refund ───────────────────────────────────────────────────────
def test_a_full_card_refund_records_one_clawback():
    st = MemoryDataStore()
    ob = _outbox(st)
    svc = _svc(st, grants=ob)
    _granted_invoice(st)
    run(PaymentStore(st).save(Payment(id="pay_1", tenant="acme", invoice_id="inv_1",
                                      provider_id="prov_1", status="succeeded", currency="SAR")))
    run(svc.refund("pay_1"))
    assert _has_clawback(st)


def test_disabled_by_the_operator_records_nothing():
    st = MemoryDataStore()
    svc = _svc(st, grants=_outbox(st), clawback_enabled=False)
    _granted_invoice(st)
    run(PaymentStore(st).save(Payment(id="pay_1", tenant="acme", invoice_id="inv_1",
                                      provider_id="prov_1", status="succeeded", currency="SAR")))
    run(svc.refund("pay_1"))
    assert not _has_clawback(st)


def test_a_partial_refund_reverses_only_when_opted_in():
    st = MemoryDataStore()
    svc = _svc(st, grants=_outbox(st), on_partial=False)
    _granted_invoice(st, total=5000)
    run(PaymentStore(st).save(Payment(id="pay_1", tenant="acme", invoice_id="inv_1",
                                      provider_id="prov_1", status="succeeded", currency="SAR")))
    run(svc.refund("pay_1", amount_minor=1000))       # partial
    assert not _has_clawback(st)


# ── path 2: gateway refund webhook ────────────────────────────────────────────
class _RefundGw:
    driver = "fake"

    class _E:
        verified = True
        type = "refund"
        provider_id = "prov_1"

    def verify_webhook(self, *, headers, body, secret):
        return self._E()


def test_a_refund_webhook_records_one_clawback():
    st = MemoryDataStore()
    svc = _svc(st, grants=_outbox(st), gw=_RefundGw())
    _granted_invoice(st)
    run(PaymentStore(st).save(Payment(id="pay_1", tenant="acme", invoice_id="inv_1",
                                      provider_id="prov_1", status="succeeded", currency="SAR")))
    run(svc.handle_webhook("fake", {}, b"{}", "sec"))
    assert _has_clawback(st)


# ── path 3: recalled settlement ───────────────────────────────────────────────
def test_a_reversed_settlement_that_had_paid_records_one_clawback():
    st = MemoryDataStore()
    svc = _svc(st, grants=_outbox(st))
    inv = _granted_invoice(st)
    inv.status = "paid"
    run(InvoiceStore(st).save(inv))
    stl = Settlement(id="stl_1", tenant="acme", invoice_id="inv_1", amount_minor=5000,
                     currency="SAR", status="confirmed")
    run(SettlementStore(st).save(stl))
    run(svc.reverse_settlement(settlement_id="stl_1", reason="recall"))
    assert _has_clawback(st)
