"""Round-2 audit money-bug regressions — each test FAILS on the pre-fix code.

Coverage (see docs/audits/round2/billing-service.json):
  billing-r2-01  refund() TOCTOU → concurrent DOUBLE REFUND at the gateway
  billing-r2-02  wallet top-up GATEWAY idempotency key omitted `fund`
  billing-r2-03  consolidated active-mode grouped wallet-pinned + card subs into one charge
  billing-r2-04  reverse_settlement wrote settled_minor from a stale read (lost update)
  billing-r2-07  a partial refund set pay.status='refunded', blocking a later full-refund webhook

Concurrency is forced with an InterleavingStore whose get() yields, so gathered coroutines
interleave at every read exactly as two pods/requests would.
"""
from __future__ import annotations

import asyncio

from app.adapters.datastore.memory import MemoryDataStore
from app.adapters.tax.vat import VatCalculator
from app.core.billing_service import BillingService
from app.core.models import Invoice, Payment, Subscription, now_ts
from app.core.stores import (CouponStore, IdempotencyStore, InvoiceStore, LedgerStore, PaymentStore,
                             PlanStore, SettlementStore, SubscriptionStore, UsageStore)
from app.ports.payment_gateway import Charge

from tests.unit.test_billing_core import CFG, MockEInv, NullPdf


def run(c):
    return asyncio.run(c)


class InterleavingStore(MemoryDataStore):
    """A memory store whose reads YIELD control, so `asyncio.gather` of two operations interleaves
    at every get() — the way two replicas racing one row do. incr_if_below/cas stay atomic (they
    hold the lock), so this exercises the get-then-write TOCTOU windows, not the atomic primitives."""

    async def get(self, collection, key):
        # Read the CURRENT value, THEN yield: a gathered peer resumes and reads the SAME (stale)
        # value before this coroutine writes it back — the exact get-then-put TOCTOU window. Yielding
        # before the read would instead hand the peer a fresh post-write value and hide the race.
        row = await super().get(collection, key)
        await asyncio.sleep(0)
        return row


# ── billing-r2-01: refund() must reach the gateway EXACTLY once under concurrency ──────────────
class _CountingRefundGW:
    driver = "mock"

    def __init__(self):
        self.refunds = 0
        self.total_refunded = 0

    def charge(self, **k):
        return Charge(ok=True, status="succeeded", provider="mock", provider_id="ch_1",
                      amount_minor=k["amount_minor"], currency=k["currency"])

    def refund(self, **k):
        self.refunds += 1
        self.total_refunded += int(k.get("amount_minor") or 0)
        return Charge(ok=True, status="refunded", provider="mock", provider_id="re_1")


def _refund_svc(st, gw):
    return BillingService(plans=PlanStore(st), coupons=CouponStore(st), subs=SubscriptionStore(st),
                          invoices=InvoiceStore(st), payments=PaymentStore(st), usage=UsageStore(st),
                          ledger=LedgerStore(st), idempo=IdempotencyStore(st),
                          settlements=SettlementStore(st), gateway=gw, tax=VatCalculator("0.15"),
                          einvoicer=MockEInv(), pdf=NullPdf(), config=CFG)


def _paid(st, iid="inv_1", total=20000):
    run(InvoiceStore(st).save(Invoice(id=iid, tenant="t1", number=iid, status="paid",
                                      total_minor=total, currency="SAR")))
    run(PaymentStore(st).save(Payment(id=f"pay_{iid}", tenant="t1", invoice_id=iid,
                                      status="succeeded", provider_id="ch", currency="SAR")))


def test_two_concurrent_identical_refunds_hit_the_gateway_once():
    # Pre-fix (get-then-put, no claim): both reads see refunded_minor=0, both pass the outstanding
    # guard, both call gw.refund() → the customer is refunded TWICE. The atomic claim admits one.
    st = InterleavingStore()
    gw = _CountingRefundGW()
    b = _refund_svc(st, gw)
    _paid(st, "inv_1", 20000)

    async def both():
        return await asyncio.gather(b.refund("pay_inv_1", amount_minor=20000),
                                    b.refund("pay_inv_1", amount_minor=20000),
                                    return_exceptions=True)

    results = run(both())
    assert gw.refunds == 1, f"gateway refunded {gw.refunds} times: {results}"
    inv = run(InvoiceStore(st).fetch("inv_1"))
    assert inv.refunded_minor == 20000 and inv.status == "void"
    # exactly one call reports the real refund; the duplicate replays / is refused, never a 2nd refund
    oks = [r for r in results if isinstance(r, dict) and r.get("ok") and r.get("status") == "refunded"]
    assert len(oks) == 1, results


def test_refund_counter_bounds_two_concurrent_partial_refunds_to_the_total():
    # Two DIFFERENT concurrent partials can never together return more than was charged. Pre-fix both
    # read refunded_minor=0, both pass the outstanding guard, and 15000 + 8000 = 23000 leaves the
    # gateway on a 20000 invoice (over-refund). The atomic counter caps the total that can go back.
    st = InterleavingStore()
    gw = _CountingRefundGW()
    b = _refund_svc(st, gw)
    _paid(st, "inv_2", 20000)

    async def both():
        return await asyncio.gather(b.refund("pay_inv_2", amount_minor=15000),
                                    b.refund("pay_inv_2", amount_minor=8000),
                                    return_exceptions=True)

    run(both())
    assert gw.total_refunded <= 20000, f"over-refunded: {gw.total_refunded}"
    assert run(InvoiceStore(st).fetch("inv_2")).refunded_minor <= 20000


# ── billing-r2-02: the GATEWAY idempotency key must include `fund` ─────────────────────────────
class _KeyCaptureGW:
    driver = "fake"

    def __init__(self):
        self.keys = []

    def charge(self, **k):
        self.keys.append(k["idempotency_key"])
        return Charge(ok=True, status="succeeded", provider="fake", provider_id="prov_" + str(len(self.keys)),
                      amount_minor=k["amount_minor"], currency=k["currency"])


def _topup_svc(st, gw):
    from app.core.grants import GrantOutbox
    from app.core.models import iso
    cfg = dict(CFG, wallet_topup_fund="", wallet_topup_tax_mode="receipt", wallet_topup_max_minor=0)
    ob = GrantOutbox(st, None, clock=now_ts, iso=iso,
                     cfg={"grant_fund": "credits", "grant_retry_base_seconds": 30,
                          "grant_retry_max_seconds": 3600})
    return BillingService(plans=PlanStore(st), coupons=CouponStore(st), subs=SubscriptionStore(st),
                          invoices=InvoiceStore(st), payments=PaymentStore(st), usage=UsageStore(st),
                          ledger=LedgerStore(st), idempo=IdempotencyStore(st),
                          settlements=SettlementStore(st), gateway=gw, tax=VatCalculator("0.15"),
                          einvoicer=MockEInv(), pdf=NullPdf(), config=cfg, grants=ob)


def test_topup_gateway_key_distinguishes_the_fund():
    # Same tenant + token + amount, DIFFERENT fund: pre-fix both charges carried the SAME gateway
    # idempotency key (fund omitted), so a deduping gateway charges once while the app mints a second
    # invoice and credits the second fund — charged once, two funds credited (free stored value).
    st = MemoryDataStore()
    gw = _KeyCaptureGW()
    b = _topup_svc(st, gw)
    run(b.wallet_topup(tenant="acme", amount_minor=10_000, payment_token="tok", fund="fundA"))
    run(b.wallet_topup(tenant="acme", amount_minor=10_000, payment_token="tok", fund="fundB"))
    assert len(gw.keys) == 2 and gw.keys[0] != gw.keys[1], gw.keys
    assert gw.keys[0].endswith(":fundA") and gw.keys[1].endswith(":fundB")


# ── billing-r2-03: consolidated active-mode must NOT group wallet-pinned with card subs ─────────
class _RecordingGW:
    driver = "moyasar"

    def __init__(self):
        self.charged = []

    def charge(self, **k):
        self.charged.append(k)
        return Charge(ok=True, status="succeeded", provider="moyasar", provider_id="ch_" + k["idempotency_key"][:6],
                      amount_minor=k["amount_minor"], currency=k["currency"])


class _Wallet:
    def __init__(self):
        self.debits = []

    async def debit(self, *, tenant, fund, amount_minor, source, reason="", allow_negative=False):
        self.debits.append({"fund": fund, "amount_minor": amount_minor, "source": source})
        return True

    async def credit(self, **k):
        return True

    async def health(self):
        return True


def _consol_svc(st, gw, wallet):
    cfg = dict(CFG, renewal_invoice_mode="consolidated", renewal_gateway_mode="active",
               wallet_renewal_enabled=True, wallet_renewal_fund="renewal", cycle_sweep_limit=1000)
    return BillingService(plans=PlanStore(st), coupons=CouponStore(st), subs=SubscriptionStore(st),
                          invoices=InvoiceStore(st), payments=PaymentStore(st), usage=UsageStore(st),
                          ledger=LedgerStore(st), idempo=IdempotencyStore(st),
                          settlements=SettlementStore(st), gateway=gw, tax=VatCalculator("0.15"),
                          einvoicer=MockEInv(), pdf=NullPdf(), config=cfg, wallet=wallet)


def _due_sub(sub_id, gateway, amount, created):
    return Subscription(
        id=sub_id, tenant="t1", plan_id="plan_x", interval="month", quantity=1, status="active",
        payment_token="tok", payment_gateway=gateway,
        price_snapshot={"interval": "month", "amount_minor": amount, "currency": "SAR", "type": "flat"},
        limits_snapshot={}, current_period_start=1.0, current_period_end=2.0, created=created)


def test_consolidated_active_never_debits_a_card_sub_from_the_wallet():
    # A wallet-pinned sub and a card sub, both due, consolidated + active mode. Pre-fix the grouping
    # key was "" for both, so they shared ONE invoice charged via claimed[0]; with the wallet sub
    # first, the CARD sub's dues were debited from the wallet and the gateway was never touched.
    st = MemoryDataStore()
    gw, w = _RecordingGW(), _Wallet()
    b = _consol_svc(st, gw, w)
    run(st.put("plans", "plan_x", {"id": "plan_x", "name": "Pro", "active": True,
        "prices": [{"interval": "month", "amount_minor": 10000, "currency": "SAR", "type": "flat"}],
        "features": [], "limits": {}}))
    # wallet sub created FIRST (oldest) → it would be the group representative pre-fix
    run(SubscriptionStore(st).save(_due_sub("sub_wallet", "wallet", 10000, "2026-01-01T00:00:00")))
    run(SubscriptionStore(st).save(_due_sub("sub_card", "moyasar", 20000, "2026-01-02T00:00:00")))

    run(b.cycle_due_sweep())

    # the card sub's renewal MUST have reached the gateway, never the wallet
    assert gw.charged, "card sub was not charged on the gateway (grouped into the wallet debit)"
    # the wallet was debited only ONCE, and for the wallet sub's own total (10000 + 15% VAT = 11500)
    assert len(w.debits) == 1, w.debits
    assert w.debits[0]["amount_minor"] == 11500, w.debits


# ── billing-r2-04: reverse_settlement keeps the row's settled_minor in step with the counter ─────
def test_concurrent_reversals_leave_the_row_settled_minor_equal_to_the_counter():
    # INVARIANT lock-in: after concurrent reversals the row's settled_minor must equal the authoritative
    # counter and the invoice must be derivable as open. The fix sources settled_minor from a fresh
    # counter read (never the stale local incr-return the audit flagged) and re-fetches the row before
    # the status write. (A deterministically RED-on-pre-fix concurrency assertion is not achievable
    # with the in-memory harness — the same limitation tests/unit/test_concurrency_races.py documents —
    # so this guards the invariant; the settlement suite covers the single-writer paths.)
    from tests.unit.test_settlement import _svc as _settle_svc, _invoice, _settle
    st = InterleavingStore()
    svc, _ = _settle_svc(st)
    inv = run(_invoice(svc, total=100_000))
    a = _settle(svc, inv, reference="T1", amount_minor=40_000)
    _settle(svc, inv, reference="T2", amount_minor=60_000)
    sid_a = a["settlement"]["id"]
    b = run(svc._settlements.for_invoice(inv.id))
    sid_b = next(r["id"] for r in b if r["reference"] == "T2")

    async def both():
        return await asyncio.gather(svc.reverse_settlement(settlement_id=sid_a),
                                    svc.reverse_settlement(settlement_id=sid_b),
                                    return_exceptions=True)

    run(both())
    counter = run(svc.settled_minor(inv.id))
    row = run(InvoiceStore(st).fetch(inv.id))
    assert counter == 0, counter
    assert row.settled_minor == counter, f"row {row.settled_minor} != counter {counter}"
    assert run(svc.invoice_state(inv.id)) == "open"


# ── billing-r2-07: a partial refund must not block a later full-refund webhook ─────────────────
class _RefundOnlyGW:
    driver = "fake"

    class _E:
        verified = True
        type = "refund"
        provider_id = "prov_1"

    def refund(self, **k):
        return Charge(ok=True, status="refunded", provider="fake", provider_id="re_1")

    def verify_webhook(self, *, headers, body, secret):
        return self._E()


def _wh_svc(st):
    cfg = dict(CFG, webhook_secret="sec")
    return BillingService(plans=PlanStore(st), coupons=CouponStore(st), subs=SubscriptionStore(st),
                          invoices=InvoiceStore(st), payments=PaymentStore(st), usage=UsageStore(st),
                          ledger=LedgerStore(st), idempo=IdempotencyStore(st),
                          settlements=SettlementStore(st), gateway=_RefundOnlyGW(),
                          tax=VatCalculator("0.15"), einvoicer=MockEInv(), pdf=NullPdf(), config=cfg)


def test_partial_api_refund_then_gateway_full_refund_webhook_voids():
    # 30% back via the API, then the merchant refunds the remaining 70% at the gateway. Pre-fix the
    # partial set pay.status='refunded', so _apply_outcome's `pay.status != 'refunded'` guard skipped
    # the webhook and the invoice never voided. Amount-based effect fixes it.
    st = MemoryDataStore()
    b = _wh_svc(st)
    run(InvoiceStore(st).save(Invoice(id="inv_1", tenant="t1", number="INV-1", status="paid",
                                      total_minor=10000, currency="SAR")))
    run(PaymentStore(st).save(Payment(id="pay_1", tenant="t1", invoice_id="inv_1",
                                      status="succeeded", provider_id="prov_1", currency="SAR")))
    run(b.refund("pay_1", amount_minor=3000))                 # partial 30%
    assert run(InvoiceStore(st).fetch("inv_1")).status == "paid"

    # the gateway confirms a full refund of the remainder via a webhook
    res = run(b.handle_webhook("fake", {}, b'{"type":"refund","pid":"prov_1"}', "sec"))
    inv = run(InvoiceStore(st).fetch("inv_1"))
    assert inv.status == "void" and inv.refunded_minor == 10000, res
