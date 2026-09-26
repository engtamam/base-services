"""Audit FIN-001 / FIN-002 — a refund must reverse EXACTLY what the payment granted.

Before the fix, _claw_back read only the single `grant_minor` field, so:
  * FIN-001 — cart / consolidated / hosted-cart invoices (grants in meta["grants"]) reversed NOTHING:
    the customer kept the wallet credits AND got the money back.
  * FIN-002 — a per-invoice grant_fund override (e.g. a wallet top-up's "topup" fund) was clawed from
    the default fund, leaving the purchased credit untouched and driving the wrong fund negative.

These tests drive the real BillingService reversal + the outbox delivery + the self-heal reconciler.
"""
from __future__ import annotations

import asyncio

from app.adapters.datastore.memory import MemoryDataStore
from app.core.billing_service import BillingService
from app.core.grants import (DELIVERED, GrantOutbox, clawback_source_for, grant_items,
                             clawback_items, source_for)
from app.core.models import Invoice, Payment, iso, now_ts
from app.core.stores import (CouponStore, IdempotencyStore, InvoiceStore, LedgerStore, PaymentStore,
                             PlanStore, SettlementStore, SubscriptionStore, UsageStore)


def run(c):
    return asyncio.run(c)


class _Gateway:
    driver = "fake"

    class _R:
        ok = True
        status = "refunded"
        provider = "fake"
        provider_id = "rf_1"

    def refund(self, *, provider_id, amount_minor=None, currency="SAR"):
        return self._R()


class FakeWallet:
    """Captures the actual per-fund debits/credits the outbox delivers."""
    def __init__(self):
        self.debits: list[dict] = []
        self.credits: list[dict] = []

    async def debit(self, *, tenant, fund, amount_minor, source, reason="", allow_negative=False):
        self.debits.append({"tenant": tenant, "fund": fund, "amount_minor": amount_minor,
                            "source": source, "allow_negative": allow_negative})

    async def credit(self, *, tenant, fund, amount_minor, source, reason="", expires_at=None):
        self.credits.append({"tenant": tenant, "fund": fund, "amount_minor": amount_minor,
                            "source": source})


def _outbox(st, wallet=None):
    return GrantOutbox(st, wallet, clock=now_ts, iso=iso,
                       cfg={"grant_fund": "credits", "grant_retry_base_seconds": 30,
                            "grant_retry_max_seconds": 3600})


def _svc(st, grants, *, grant_mode="accumulate", on_partial=False):
    cfg = {"currency": "SAR", "trial_days": 0, "vat_rate": "0.15", "reverse_charge": False,
           "grandfather": True, "dunning_base_hours": 24, "dunning_max_attempts": 4, "grace_days": 7,
           "list_limit": 100, "limits_cache_ttl": 300, "billable_statuses": ("active",),
           "max_subscriptions": 200, "merge_strategies": {}, "rank_orders": {},
           "period_tolerance_seconds": 86400, "grant_fund": "credits",
           "plan_credit_grant_mode": grant_mode, "clawback_enabled": True,
           "clawback_allow_negative": True, "clawback_on_partial": on_partial,
           "clawback_skip_expired": True}
    return BillingService(
        plans=PlanStore(st), coupons=CouponStore(st), subs=SubscriptionStore(st),
        invoices=InvoiceStore(st), payments=PaymentStore(st), usage=UsageStore(st),
        ledger=LedgerStore(st), idempo=IdempotencyStore(st), gateway=_Gateway(), tax=None,
        einvoicer=None, pdf=None, settlements=SettlementStore(st), grants=grants, config=cfg)


def _clawbacks(st):
    rows, _ = run(st.find("credit_grants", {"kind": "clawback"}, limit=50))
    return rows


def _cart_invoice(st, iid="inv_cart", grants=(("sub_A", "credits", 1000), ("sub_B", "credits", 500))):
    meta = {"grants": [{"sub_id": s, "fund": f, "amount_minor": a, "period_end": 0} for s, f, a in grants]}
    inv = Invoice(id=iid, tenant="acme", number="INV-CART", status="paid",
                  total_minor=sum(a for _, _, a in grants), currency="SAR", meta=meta)
    run(InvoiceStore(st).save(inv))
    return inv


# ── the shared reading (grant_items) mirrors what _mark_paid credits ──────────────

def test_grant_items_reads_both_shapes():
    # single grant_minor + fund override
    assert grant_items({"grant_minor": 2000, "grant_fund": "topup"}, "credits") == \
        [{"fund": "topup", "amount_minor": 2000, "period_end": 0.0, "discriminator": ""}]
    # per-sub list, per-fund
    items = grant_items({"grants": [{"sub_id": "s1", "fund": "credits", "amount_minor": 100, "period_end": 0},
                                    {"sub_id": "s2", "amount_minor": 50, "period_end": 0}]}, "credits")
    assert items == [
        {"fund": "credits", "amount_minor": 100, "period_end": 0.0, "discriminator": "s1"},
        {"fund": "credits", "amount_minor": 50, "period_end": 0.0, "discriminator": "s2"}]


# ── FIN-001: cart / consolidated grants ARE reversed ──────────────────────────────

def test_cart_refund_reverses_every_per_sub_grant():
    st = MemoryDataStore(); wallet = FakeWallet(); ob = _outbox(st, wallet); b = _svc(st, ob)
    inv = _cart_invoice(st)
    pay = Payment(id="pay_1", tenant="acme", invoice_id=inv.id, amount_minor=1500,
                  currency="SAR", status="succeeded", provider_id="ch_1")
    run(PaymentStore(st).save(pay))
    out = run(b.refund("pay_1"))
    assert out["ok"] is True
    cbs = _clawbacks(st)
    assert len(cbs) == 2                                   # one per subscription (pre-fix: ZERO)
    by_disc = {r["source"]: r for r in cbs}
    assert clawback_source_for(inv.id) + ":sub_A" in by_disc
    assert clawback_source_for(inv.id) + ":sub_B" in by_disc
    assert by_disc[clawback_source_for(inv.id) + ":sub_A"]["amount_minor"] == 1000
    assert by_disc[clawback_source_for(inv.id) + ":sub_B"]["amount_minor"] == 500
    # deliver them: the wallet is actually debited 1000 + 500 on fund credits
    for r in cbs:
        from app.core.grants import Grant
        run(ob.deliver(Grant.from_dict(r)))
    assert sorted(d["amount_minor"] for d in wallet.debits) == [500, 1000]
    assert {d["fund"] for d in wallet.debits} == {"credits"}


def test_cart_grants_on_different_funds_claw_the_right_fund_each():
    st = MemoryDataStore(); wallet = FakeWallet(); ob = _outbox(st, wallet); b = _svc(st, ob)
    inv = _cart_invoice(st, iid="inv_mix",
                        grants=(("sub_A", "credits", 1000), ("sub_B", "topup", 500)))
    pay = Payment(id="pay_1", tenant="acme", invoice_id="inv_mix", amount_minor=1500,
                  currency="SAR", status="succeeded", provider_id="ch_1")
    run(PaymentStore(st).save(pay))
    run(b.refund("pay_1"))
    from app.core.grants import Grant
    for r in _clawbacks(st):
        run(ob.deliver(Grant.from_dict(r)))
    got = {(d["fund"], d["amount_minor"]) for d in wallet.debits}
    assert got == {("credits", 1000), ("topup", 500)}     # each fund reversed for its own amount


# ── FIN-002: single grant with a fund override ────────────────────────────────────

def test_topup_refund_claws_the_topup_fund_not_the_default():
    st = MemoryDataStore(); wallet = FakeWallet(); ob = _outbox(st, wallet); b = _svc(st, ob)
    inv = Invoice(id="inv_top", tenant="acme", number="INV-TOP", status="paid", total_minor=2000,
                  currency="SAR", meta={"grant_minor": 2000, "grant_fund": "topup", "topup": True})
    run(InvoiceStore(st).save(inv))
    pay = Payment(id="pay_1", tenant="acme", invoice_id="inv_top", amount_minor=2000,
                  currency="SAR", status="succeeded", provider_id="ch_1")
    run(PaymentStore(st).save(pay))
    run(b.refund("pay_1"))
    cbs = _clawbacks(st)
    assert len(cbs) == 1 and cbs[0]["fund"] == "topup"    # pre-fix: "credits" (wrong fund)
    from app.core.grants import Grant
    run(ob.deliver(Grant.from_dict(cbs[0])))
    assert wallet.debits == [{"tenant": "acme", "fund": "topup", "amount_minor": 2000,
                              "source": clawback_source_for("inv_top"), "allow_negative": True}]


# ── reset-expired is dropped PER ITEM on its own period_end ────────────────────────

def test_reset_expired_grant_is_skipped_per_item():
    st = MemoryDataStore(); ob = _outbox(st); b = _svc(st, ob, grant_mode="reset")
    past, future = now_ts() - 10, now_ts() + 10_000
    inv = Invoice(id="inv_r", tenant="acme", number="INV-R", status="paid", total_minor=300,
                  currency="SAR",
                  meta={"grants": [{"sub_id": "s_old", "fund": "credits", "amount_minor": 100, "period_end": past},
                                   {"sub_id": "s_new", "fund": "credits", "amount_minor": 200, "period_end": future}]})
    run(InvoiceStore(st).save(inv))
    pay = Payment(id="pay_1", tenant="acme", invoice_id="inv_r", amount_minor=300,
                  currency="SAR", status="succeeded", provider_id="ch_1")
    run(PaymentStore(st).save(pay))
    run(b.refund("pay_1"))
    cbs = _clawbacks(st)
    assert len(cbs) == 1                                   # only the un-expired grant is reversed
    assert cbs[0]["amount_minor"] == 200 and cbs[0]["source"].endswith(":s_new")


# ── idempotency: a double refund reverses once per subscription ────────────────────

def test_double_refund_reverses_each_grant_once():
    st = MemoryDataStore(); ob = _outbox(st); b = _svc(st, ob)
    inv = _cart_invoice(st)
    pay = Payment(id="pay_1", tenant="acme", invoice_id=inv.id, amount_minor=1500,
                  currency="SAR", status="succeeded", provider_id="ch_1")
    run(PaymentStore(st).save(pay))
    run(b.refund("pay_1"))
    run(b.refund("pay_1"))                                 # duplicate
    assert len(_clawbacks(st)) == 2                        # still exactly one per sub, not four


# ── self-heal: reconcile_clawbacks rebuilds per-sub reversals for a voided cart ─────

def test_reconcile_clawbacks_rebuilds_per_sub_for_a_voided_cart_invoice():
    st = MemoryDataStore(); ob = _outbox(st)
    inv = _cart_invoice(st, iid="inv_v")
    run(InvoiceStore(st).save(Invoice.from_dict({**inv.to_dict(), "status": "void"})))
    # the two per-sub grants were delivered (as _mark_paid would have)
    for sub_id, fund, amt in (("sub_A", "credits", 1000), ("sub_B", "credits", 500)):
        g = run(ob.record(tenant="acme", invoice_id="inv_v", fund=fund, amount_minor=amt,
                          discriminator=sub_id))
        g.status = DELIVERED
        run(st.put("credit_grants", g.id, g.to_dict()))
    stats = run(ob.reconcile_clawbacks(invoices=InvoiceStore(st)))
    assert stats["created"] == 2                           # both per-sub reversals rebuilt
    cbs = _clawbacks(st)
    assert {r["source"] for r in cbs} == {clawback_source_for("inv_v") + ":sub_A",
                                          clawback_source_for("inv_v") + ":sub_B"}
