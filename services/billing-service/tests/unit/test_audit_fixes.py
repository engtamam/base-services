"""Regression tests for the 2026-09-12 fleet-audit findings on billing-service.

Each test would FAIL on the pre-fix code and passes only with the remediation:
  01  cross-tenant usage injection — record_usage rejects a foreign subscription id, and
      overage aggregation is tenant-scoped so an injected row can never reach a victim's invoice.
  02  refund idempotency/bounds — a repeat refund does not move money twice, and an over-amount
      refund is rejected instead of driving refunded_minor past the total.
  03  webhook fail-closed — an empty configured secret rejects outright instead of accepting a forgery.
  04  wallet top-up idempotency — a duplicate request credits the fund exactly once.
"""
from __future__ import annotations

import asyncio

from app.adapters.datastore.memory import MemoryDataStore
from app.core.billing_service import BillingService
from app.core.grants import GrantOutbox
from app.core.models import iso, now_ts
from app.core.stores import (CouponStore, DiscountRuleStore, IdempotencyStore, InvoiceStore,
                             LedgerStore, PaymentStore, PlanStore, SettlementStore,
                             SubscriptionStore, UsageStore)


def run(c):
    return asyncio.run(c)


class _Charge:
    def __init__(self, k):
        self.ok, self.status, self.provider = True, "succeeded", "mock"
        self.provider_id = "ch_" + k.get("idempotency_key", "x")[:12]
        self.amount_minor = k.get("amount_minor", 0)
        self.currency, self.error, self.raw = k.get("currency", "SAR"), "", {}


class _Refund:
    def __init__(self, ok=True):
        self.ok, self.status = ok, "refunded" if ok else "failed"


class MockGW:
    driver = "mock"

    def __init__(self):
        self.refund_calls = 0

    def charge(self, **k):
        return _Charge(k)

    def refund(self, **k):
        self.refund_calls += 1
        return _Refund(True)

    def verify_webhook(self, **k):
        # If the fail-closed guard works, this is NEVER reached for an empty secret. Return a
        # would-accept event so a test that DID reach it (the bug) would wrongly verify.
        class _E:
            verified, type, provider_id, event_id = True, "payment_paid", "", ""
        return _E()


class MockTax:
    def compute(self, *, amount_minor: int) -> int:
        from decimal import ROUND_HALF_UP, Decimal
        return int((Decimal(amount_minor) * Decimal("0.15")).quantize(Decimal(1), ROUND_HALF_UP))


def _svc(st, gw=None):
    cfg = {"currency": "SAR", "trial_days": 0, "vat_rate": "0.15", "reverse_charge": False,
           "grandfather": True, "dunning_base_hours": 24, "dunning_max_attempts": 4, "grace_days": 7,
           "list_limit": 100, "limits_cache_ttl": 300, "billable_statuses": ("active",),
           "max_subscriptions": 200, "merge_strategies": {}, "rank_orders": {},
           "period_tolerance_seconds": 86400, "cycle_sweep_limit": 1000, "cycle_lead_seconds": 0,
           "platform_max_pages": 50, "grant_fund": "credits", "plan_credit_limit_key": "credits",
           "plan_credit_grant_mode": "accumulate", "document_mode": "receipt",
           "renewal_invoice_mode": "per_subscription", "renewal_gateway_mode": "active",
           "wallet_topup_fund": "credits", "wallet_topup_tax_mode": "receipt"}
    return BillingService(
        plans=PlanStore(st), coupons=CouponStore(st), discount_rules=DiscountRuleStore(st),
        subs=SubscriptionStore(st), invoices=InvoiceStore(st), payments=PaymentStore(st),
        usage=UsageStore(st), ledger=LedgerStore(st), idempo=IdempotencyStore(st),
        settlements=SettlementStore(st), gateway=gw or MockGW(), tax=MockTax(), einvoicer=None,
        pdf=None,
        grants=GrantOutbox(st, None, clock=now_ts, iso=iso,
                           cfg={"grant_fund": "credits", "grant_retry_base_seconds": 30,
                                "grant_retry_max_seconds": 3600}),
        config=cfg)


def _metered_plan(b):
    return run(b.create_plan(name="Metered", prices=[{"interval": "month", "amount_minor": 1000,
               "currency": "SAR", "type": "flat", "usage_metric": "calls",
               "overage_minor": 5, "included_units": 0}], limits={}))


# ── 01: cross-tenant usage injection ──────────────────────────────────────────
def test_record_usage_rejects_a_foreign_subscription():
    st = MemoryDataStore(); b = _svc(st)
    p = _metered_plan(b)
    sub = run(b.subscribe(tenant="victim", plan_id=p.id, payment_token="pm_1"))
    # An attacker tenant posting against the victim's subscription id must be rejected outright.
    raised = False
    try:
        run(b.record_usage(tenant="attacker", subscription_id=sub.id, metric="calls", quantity=1_000_000))
    except ValueError as e:
        raised = True
        assert "SUB_NOT_FOUND" in str(e)
    assert raised, "cross-tenant usage write was accepted"
    # Nothing was written, so the victim's overage is zero.
    units = run(b._usage.aggregate("victim", sub.id, "calls", 0, now_ts() + 10))
    assert units == 0


def test_overage_aggregate_is_tenant_scoped():
    st = MemoryDataStore(); b = _svc(st)
    # A stray row for the same subscription id under a different tenant must never be summed in.
    run(b._usage.record("attacker", "sub_x", "calls", 999))
    run(b._usage.record("victim", "sub_x", "calls", 3))
    assert run(b._usage.aggregate("victim", "sub_x", "calls", 0, now_ts() + 10)) == 3


# ── 02: refund idempotency + bounds ───────────────────────────────────────────
def test_refund_is_idempotent_and_bounded():
    st = MemoryDataStore(); gw = MockGW(); b = _svc(st, gw)
    top = run(b.wallet_topup(tenant="t1", amount_minor=10000, payment_token="pm_1", fund="credits"))
    # find the payment for that invoice
    pays, _ = run(b._payments.browse("t1", limit=10))
    pay_id = pays[0]["id"]

    r1 = run(b.refund(pay_id))
    assert r1["ok"] and gw.refund_calls == 1
    inv = run(b._invoices.fetch(top["invoice_id"]))
    assert inv.refunded_minor == 10000

    # A second refund must NOT hit the gateway again nor add a second negative ledger row.
    r2 = run(b.refund(pay_id))
    assert r2["ok"] and r2.get("status") == "already_refunded"
    assert gw.refund_calls == 1
    inv2 = run(b._invoices.fetch(top["invoice_id"]))
    assert inv2.refunded_minor == 10000     # not doubled


def test_refund_over_outstanding_is_rejected():
    st = MemoryDataStore(); gw = MockGW(); b = _svc(st, gw)
    top = run(b.wallet_topup(tenant="t1", amount_minor=10000, payment_token="pm_1", fund="credits"))
    pays, _ = run(b._payments.browse("t1", limit=10))
    res = run(b.refund(pays[0]["id"], amount_minor=15000))
    assert not res["ok"] and res["reason"] == "REFUND_EXCEEDS_OUTSTANDING"
    assert gw.refund_calls == 0              # never reached the gateway


# ── 03: webhook fail-closed on an empty secret ────────────────────────────────
def test_webhook_rejects_when_no_secret_configured():
    st = MemoryDataStore(); b = _svc(st)
    res = run(b.handle_webhook("moyasar", headers={}, body=b'{"type":"payment_paid"}', secret=""))
    assert not res["ok"] and res["reason"] == "WEBHOOK_SECRET_NOT_CONFIGURED"


# ── 04: wallet top-up idempotency ─────────────────────────────────────────────
def test_duplicate_wallet_topup_credits_once():
    st = MemoryDataStore(); b = _svc(st)
    a = run(b.wallet_topup(tenant="t1", amount_minor=5000, payment_token="pm_dup", fund="credits"))
    c = run(b.wallet_topup(tenant="t1", amount_minor=5000, payment_token="pm_dup", fund="credits"))
    assert c.get("duplicate") is True
    assert c["invoice_id"] == a["invoice_id"]        # same invoice replayed, no second one
    invs, _ = run(b._invoices.browse("t1", limit=10))
    assert len(invs) == 1                              # exactly one invoice for one payment
