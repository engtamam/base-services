"""Unit: pricing, coupons, proration, tax, numbering, dunning, webhook, gateway shapes."""
from __future__ import annotations

import asyncio

import pytest

from app.adapters.datastore.memory import MemoryDataStore
from app.adapters.tax.vat import VatCalculator
from app.core import pricing
from app.core.billing_service import BillingService
from app.core.stores import (CouponStore, InvoiceStore, PaymentStore, PlanStore,
                             SubscriptionStore, UsageStore)
from app.ports.payment_gateway import Charge, GatewayUnavailable, WebhookEvent

CFG = {"cycle_sweep_limit": 1000, "platform_max_pages": 50, "cycle_lead_seconds": 0, "grant_fund": "credits", "plan_credit_limit_key": "credits",
       "plan_credit_grant_mode": "accumulate", "document_mode": "tax_invoice",
       # A COMPLETE tax profile — required now for tax_invoice mode to actually issue one.
       "tax_driver": "vat", "einvoicer_driver": "zatca-http", "seller": {"name": "ACME KSA", "vat_number": "300000000000003"},
       "currency": "SAR", "trial_days": 0, "vat_rate": "0.15", "dunning_base_hours": 24,
       "dunning_max_attempts": 3, "grace_days": 7, "list_limit": 100, "reverse_charge": False,
       "settlement_methods": {"bank_transfer": True, "cheque": True}, "settlement_allow_partial": True,
       "settlement_tolerance_minor": 0, "settlement_max_backdate_days": 3650,
       # Entitlement resolution (Phase 42). Required by EntitlementResolver — a service
       # built without these would resolve entitlements against nothing.
       "billable_statuses": ("active", "trialing", "past_due"), "grandfather": True,
       "max_subscriptions": 200, "merge_strategies": {}, "rank_orders": {}, "period_tolerance_seconds": 86400,
       }


def run(c):
    return asyncio.run(c)


# ── pricing (pure) ────────────────────────────────────────────────────────────
def test_pricing_models():
    assert pricing.line_amount({"type": "flat", "amount_minor": 10000}) == 10000
    assert pricing.line_amount({"type": "per_seat", "amount_minor": 5000}, quantity=3) == 15000
    assert pricing.line_amount({"type": "usage", "overage_minor": 10}, usage_units=250) == 2500
    assert pricing.line_amount({"type": "hybrid", "amount_minor": 10000, "included_units": 100,
                                "overage_minor": 5}, usage_units=150) == 10250


def test_coupon_discount():
    assert pricing.coupon_discount({"type": "percent", "value": 25}, 10000) == 2500
    assert pricing.coupon_discount({"type": "fixed", "value": 3000}, 10000) == 3000
    assert pricing.coupon_discount({"type": "fixed", "value": 99999}, 10000) == 10000   # capped
    assert pricing.coupon_discount({"type": "trial", "value": 30}, 10000) == 0
    assert pricing.coupon_discount(None, 10000) == 0


def test_proration():
    # exactly half the period remaining: upgrade 10000 -> 20000 nets +5000
    d = pricing.proration_delta(10000, 20000, period_start=0, period_end=100, now=50)
    assert d == 5000


def test_vat():
    t = VatCalculator("0.15")
    assert t.compute(amount_minor=10000) == 1500
    assert t.compute(amount_minor=10000, exempt=True) == 0
    assert t.compute(amount_minor=10000, reverse_charge=True) == 0


# ── proration primitives for aligning a second subscription (pure) ──────────────
def test_recurring_component_is_the_prorated_part_only():
    from app.core import pricing as p
    assert p.recurring_component({"type": "flat", "amount_minor": 10000}) == 10000
    assert p.recurring_component({"type": "per_seat", "amount_minor": 5000}, quantity=3) == 15000
    assert p.recurring_component({"type": "usage", "overage_minor": 10}) == 0          # never prorated
    # hybrid: base is recurring, overage is metered usage (not prorated)
    assert p.recurring_component({"type": "hybrid", "amount_minor": 10000, "overage_minor": 5}) == 10000


def test_prorate_is_deterministic_half_up_and_clamped():
    from decimal import Decimal
    from app.core import pricing as p
    assert p.prorate(10000, Decimal(16) / Decimal(30)) == 5333       # 5333.33 -> half-up 5333
    assert p.prorate(10000, Decimal(1)) == 10000                     # full period unchanged
    assert p.prorate(10000, Decimal(0)) == 0
    assert p.prorate(10000, Decimal(2)) == 10000                     # clamped to the amount
    assert p.prorate(10000, Decimal(-1)) == 0                        # clamped to zero
    assert p.prorate(101, Decimal(1) / Decimal(2)) == 51            # 50.5 -> half-up 51


def test_aligned_period_end_lands_on_the_existing_grid():
    from app.core import periods
    day = 86400
    month = 30 * day
    # A subscription's period ends at month; a second bought 14 days in aligns to that SAME boundary.
    assert periods.aligned_period_end(14 * day, month, month) == month
    # Bought the instant before the boundary → still that boundary (a tiny partial period).
    assert periods.aligned_period_end(month - 1, month, month) == month
    # Bought exactly on a boundary → the NEXT one (a full period, never zero-length).
    assert periods.aligned_period_end(month, month, month) == 2 * month
    # A non-renewing interval has nothing to align to.
    assert periods.aligned_period_end(100.0, 0, 0) == 100.0
    # Every result advances by a full interval from there — so two subs stay aligned for ever.
    e = periods.aligned_period_end(14 * day, month, month)
    assert (e + month - e) == month


def test_distinct_renewing_period_ends_is_the_invariant():
    from app.core import periods
    subs = [{"interval": "month", "status": "active", "current_period_end": 2592000.0},
            {"interval": "month", "status": "active", "current_period_end": 2592000.0},
            {"interval": "month", "status": "trialing", "current_period_end": 111.0},   # excluded
            {"interval": "once", "status": "active", "current_period_end": 0.0},        # excluded
            {"interval": "year", "status": "active", "current_period_end": 9999.0}]     # other grid
    assert periods.distinct_renewing_period_ends(subs, "month") == {2592000}
    assert periods.distinct_renewing_period_ends(subs, "year") == {9999}


# ── stores ────────────────────────────────────────────────────────────────────
def test_sequential_invoice_numbers():
    st = InvoiceStore(MemoryDataStore())
    nums = [run(st.next_number("t1")) for _ in range(3)]
    assert nums == ["INV-000001", "INV-000002", "INV-000003"]
    assert run(st.next_number("t2")) == "INV-000001"           # per-tenant sequence


# ── orchestration ─────────────────────────────────────────────────────────────
class MockGW:
    driver = "mock"

    def __init__(self, ok=True):
        self.ok = ok

    def charge(self, **k):
        return Charge(ok=self.ok, status="succeeded" if self.ok else "failed", provider="mock",
                      provider_id="ch_" + k["idempotency_key"][:6], amount_minor=k["amount_minor"],
                      currency=k["currency"], error="" if self.ok else "declined")

    def refund(self, **k):
        return Charge(ok=True, status="refunded", provider="mock", provider_id="re_1")

    def verify_webhook(self, **k):
        import json
        try:
            e = json.loads(k["body"])
        except Exception:
            return WebhookEvent(verified=False)
        return WebhookEvent(verified=k["secret"] == "good", type=e.get("type", ""), provider_id=e.get("pid", ""))


class MockEInv:
    driver = "zatca"

    def __init__(self):
        self.calls = []

    def finalize(self, *, tenant, invoice):
        self.calls.append((tenant, invoice["number"]))
        return {"status": "reported"}


class NullPdf:
    driver = "null"

    def render(self, inv):
        return None


def _svc(st, gw, einv=None):
    from app.core.stores import IdempotencyStore, LedgerStore, SettlementStore
    return BillingService(plans=PlanStore(st), coupons=CouponStore(st), subs=SubscriptionStore(st),
                          invoices=InvoiceStore(st), payments=PaymentStore(st), usage=UsageStore(st),
                          ledger=LedgerStore(st), idempo=IdempotencyStore(st),
                          settlements=SettlementStore(st),
                          gateway=gw, tax=VatCalculator("0.15"), einvoicer=einv or MockEInv(),
                          pdf=NullPdf(), config=CFG)


def _plan_sub(b, token="pm_1", coupon=""):
    plan = run(b.create_plan(name="Pro", prices=[{"interval": "month", "amount_minor": 10000,
                                                   "currency": "SAR", "type": "flat"}]))
    sub = run(b.subscribe(tenant="t1", plan_id=plan.id, payment_token=token, coupon_code=coupon))
    return plan, sub


def _flat_month_plan(b, name="Pro", amount=10000):
    return run(b.create_plan(name=name, prices=[{"interval": "month", "amount_minor": amount,
                                                 "currency": "SAR", "type": "flat"}]))


# ── aligning a tenant's second subscription onto ONE billing grid ────────────────
def test_a_first_subscription_is_never_aligned_or_prorated(monkeypatch):
    # A tenant's FIRST subscription has no grid to align to and is completely unchanged: a full
    # interval from now, no proration flag.
    import app.core.billing_service as bs
    b = _svc(MemoryDataStore(), MockGW(ok=True))
    monkeypatch.setattr(bs, "now_ts", lambda: 1_000_000.0)
    plan = _flat_month_plan(b)
    sub = run(b.subscribe(tenant="t1", plan_id=plan.id, payment_token="pm_1"))
    assert sub.first_period_prorated is False
    assert sub.current_period_end == 1_000_000.0 + bs._INTERVAL["month"]


def test_second_subscription_aligns_to_the_first_and_prorates_its_first_cycle(monkeypatch):
    # THE escalated defect: a second subscription must renew on the SAME day as the first, not open
    # its own cycle. It aligns to the shared grid boundary (a partial first period) and the first
    # cycle charges the recurring fee prorated to that partial period.
    import app.core.billing_service as bs
    b = _svc(MemoryDataStore(), MockGW(ok=True))
    T0, month, day = 1_000_000.0, bs._INTERVAL["month"], 86400
    monkeypatch.setattr(bs, "now_ts", lambda: T0)
    a = run(b.subscribe(tenant="t1", plan_id=_flat_month_plan(b, "A").id, payment_token="pm_1"))

    monkeypatch.setattr(bs, "now_ts", lambda: T0 + 14 * day)          # 14 days later
    bsub = run(b.subscribe(tenant="t1", plan_id=_flat_month_plan(b, "B", 6000).id, payment_token="pm_1"))
    assert bsub.first_period_prorated is True
    assert bsub.current_period_end == a.current_period_end == T0 + month     # one shared renewal day

    inv = run(b.run_cycle(bsub.id))                                  # first cycle at the boundary
    assert inv.subtotal_minor == 3200                                # 16/30 of 6000 = 3200 (taxable)
    pr = inv.lines[0]["proration"]
    assert pr["recurring_full_minor"] == 6000 and pr["period_seconds"] == 16 * day
    assert "prorated first period" in inv.lines[0]["description"]
    # the flag is consumed and the next period is a FULL interval on the shared grid
    after = run(b._subs.fetch(bsub.id))
    assert after.first_period_prorated is False
    assert round(after.current_period_end - after.current_period_start) == month


def test_the_invariant_one_renewal_day_after_first_cycle(monkeypatch):
    # The guard the escalation values above the fix: a tenant with more than one live renewing
    # subscription has exactly ONE distinct current_period_end. Proven end-to-end.
    import app.core.billing_service as bs
    from app.core import periods
    b = _svc(MemoryDataStore(), MockGW(ok=True))
    T0, month, day = 1_000_000.0, bs._INTERVAL["month"], 86400
    plan = _flat_month_plan(b)
    monkeypatch.setattr(bs, "now_ts", lambda: T0)
    a = run(b.subscribe(tenant="t1", plan_id=plan.id, payment_token="pm_1"))
    monkeypatch.setattr(bs, "now_ts", lambda: T0 + 17 * day)         # a different day
    c = run(b.subscribe(tenant="t1", plan_id=plan.id, payment_token="pm_1"))

    subs = run(b._subs.billable_for("t1", bs._BILLABLE))
    assert len(periods.distinct_renewing_period_ends(subs, "month")) == 1    # aligned at signup

    run(b.run_cycle(a.id))
    run(b.run_cycle(c.id))                                           # both first cycles
    subs = run(b._subs.billable_for("t1", bs._BILLABLE))
    assert len(periods.distinct_renewing_period_ends(subs, "month")) == 1    # and after


def test_month_and_year_subscriptions_do_not_cross_align(monkeypatch):
    # Alignment is per interval: a yearly subscription is not dragged onto a monthly grid.
    import app.core.billing_service as bs
    b = _svc(MemoryDataStore(), MockGW(ok=True))
    T0, day = 1_000_000.0, 86400
    monkeypatch.setattr(bs, "now_ts", lambda: T0)
    run(b.subscribe(tenant="t1", plan_id=_flat_month_plan(b).id, payment_token="pm_1"))
    yplan = run(b.create_plan(name="Yr", prices=[{"interval": "year", "amount_minor": 100000,
                                                  "currency": "SAR", "type": "flat"}]))
    monkeypatch.setattr(bs, "now_ts", lambda: T0 + 5 * day)
    y = run(b.subscribe(tenant="t1", plan_id=yplan.id, interval="year", payment_token="pm_1"))
    assert y.first_period_prorated is False
    assert y.current_period_end == T0 + 5 * day + bs._INTERVAL["year"]


def test_run_cycle_paid_and_einvoiced():
    st = MemoryDataStore()
    einv = MockEInv()
    b = _svc(st, MockGW(ok=True), einv)
    _, sub = _plan_sub(b)
    inv = run(b.run_cycle(sub.id))
    assert inv.status == "paid" and inv.total_minor == 11500
    assert einv.calls == [("t1", "INV-000001")] and inv.einvoice["status"] == "reported"


def test_run_cycle_failure_enters_dunning():
    st = MemoryDataStore()
    b = _svc(st, MockGW(ok=False))
    _, sub = _plan_sub(b)
    inv = run(b.run_cycle(sub.id))
    assert inv.status == "open"
    pays = [p for p in run(PaymentStore(st).list_tenant("t1", 10))]
    assert pays[0]["status"] == "failed" and pays[0]["next_retry_at"] is not None


def test_dunning_cap_suspends_subscription():
    st = MemoryDataStore()
    b = _svc(st, MockGW(ok=False))
    _, sub = _plan_sub(b)
    run(b.run_cycle(sub.id))
    pay = run(PaymentStore(st).list_tenant("t1", 10))[0]
    pid = pay["id"]
    # force it due and sweep until the cap → subscription past_due
    for _ in range(CFG["dunning_max_attempts"] + 1):
        p = run(PaymentStore(st).fetch(pid))
        if p.next_retry_at is None:
            break
        p.next_retry_at = 0
        run(PaymentStore(st).save(p))
        run(b.dunning_sweep())
    assert run(SubscriptionStore(st).fetch(sub.id)).status == "past_due"


def test_dunning_recovers_on_sweep():
    st = MemoryDataStore()
    gw = MockGW(ok=False)
    b = _svc(st, gw)
    _, sub = _plan_sub(b)
    inv = run(b.run_cycle(sub.id))
    pay = run(PaymentStore(st).list_tenant("t1", 10))[0]
    pay["next_retry_at"] = 0
    run(st.put("payments", pay["id"], pay))
    gw.ok = True                                             # gateway recovers
    assert run(b.dunning_sweep())["retried"] == 1
    assert run(InvoiceStore(st).fetch(inv.id)).status == "paid"


def test_webhook_reconcile_idempotent():
    import json
    st = MemoryDataStore()
    b = _svc(st, MockGW(ok=False))
    _, sub = _plan_sub(b)
    inv = run(b.run_cycle(sub.id))
    pid = run(PaymentStore(st).list_tenant("t1", 10))[0]["provider_id"]
    body = json.dumps({"type": "payment.succeeded", "pid": pid}).encode()
    assert run(b.handle_webhook("mock", {}, body, "bad"))["ok"] is False       # bad signature
    r = run(b.handle_webhook("mock", {}, body, "good"))
    assert r["reconciled"] and run(InvoiceStore(st).fetch(inv.id)).status == "paid"
    r2 = run(b.handle_webhook("mock", {}, body, "good"))                        # idempotent
    assert r2["payment_status"] == "succeeded"


def test_plan_scoped_coupon_refuses_the_wrong_plan_without_redeeming():
    st = MemoryDataStore()
    b = _svc(st, MockGW(ok=True))
    a = run(b.create_plan(name="A", prices=[{"interval": "month", "amount_minor": 10000,
                                             "currency": "SAR", "type": "flat"}]))
    other = run(b.create_plan(name="B", prices=[{"interval": "month", "amount_minor": 10000,
                                                 "currency": "SAR", "type": "flat"}]))
    run(b.upsert_coupon(code="AONLY", type="percent", value=50, plan_id=a.id))
    # Wrong plan → REFUSED loudly, and the refusal consumes NO redemption (checked before redeem).
    with pytest.raises(ValueError) as e:
        run(b.subscribe(tenant="t2", plan_id=other.id, coupon_code="AONLY", payment_token="pm_1"))
    assert "COUPON_PLAN_MISMATCH" in str(e.value)
    assert run(b.get_coupon("AONLY"))["redeemed"] == 0
    # Right plan → the discount applies, and now exactly one redemption is counted.
    sub = run(b.subscribe(tenant="t1", plan_id=a.id, coupon_code="AONLY", payment_token="pm_1"))
    assert run(b.run_cycle(sub.id)).discount_minor == 5000
    assert run(b.get_coupon("AONLY"))["redeemed"] == 1


def test_unscoped_coupon_still_applies_to_any_plan():
    st = MemoryDataStore()
    b = _svc(st, MockGW(ok=True))
    plan = run(b.create_plan(name="X", prices=[{"interval": "month", "amount_minor": 10000,
                                                "currency": "SAR", "type": "flat"}]))
    run(b.upsert_coupon(code="ANY", type="percent", value=10))   # no plan_id → any plan (as before)
    sub = run(b.subscribe(tenant="t1", plan_id=plan.id, coupon_code="ANY", payment_token="pm_1"))
    assert run(b.run_cycle(sub.id)).discount_minor == 1000


def test_zero_total_marks_paid_without_gateway():
    st = MemoryDataStore()
    b = _svc(st, MockGW(ok=False))                           # gateway would fail, but 100% coupon
    run(b.upsert_coupon(code="FREE", type="percent", value=100))
    plan = run(b.create_plan(name="Free", prices=[{"interval": "month", "amount_minor": 10000,
                                                    "currency": "SAR", "type": "flat"}]))
    sub = run(b.subscribe(tenant="t1", plan_id=plan.id, coupon_code="FREE"))
    inv = run(b.run_cycle(sub.id))
    assert inv.discount_minor == 10000 and inv.total_minor == 0 and inv.status == "paid"


# ── gateway request shapes (verified endpoints/units/auth) ──────────────────────
def test_gateway_request_shapes():
    import app.adapters.payment.stripe as S
    import app.adapters.payment.moyasar as M
    import app.adapters.payment.tap as T
    import app.adapters.payment.checkout as C

    seen = {}
    S.post_form = lambda u, f, h=None: (seen.update(stripe=(u, f, h)) or (200, {"id": "pi", "status": "succeeded"}))
    M.post_form = lambda u, f, h=None: (seen.update(moyasar=(u, f, h)) or (200, {"id": "p", "status": "paid"}))
    T.post_json = lambda u, p, h=None: (seen.update(tap=(u, p, h)) or (200, {"id": "c", "status": "CAPTURED"}))
    C.post_json = lambda u, p, h=None: (seen.update(checkout=(u, p, h)) or (200, {"id": "c", "status": "Authorized"}))

    assert S.StripeGateway("sk").charge(amount_minor=5000, currency="SAR", token="pm", idempotency_key="k").ok
    assert seen["stripe"][0].endswith("/v1/payment_intents") and seen["stripe"][1]["amount"] == 5000
    assert seen["stripe"][2]["Authorization"].startswith("Bearer ")
    assert M.MoyasarGateway("sk").charge(amount_minor=5000, currency="SAR", token="t", idempotency_key="k").ok
    assert seen["moyasar"][1]["amount"] == 5000 and seen["moyasar"][2]["Authorization"].startswith("Basic ")
    assert T.TapGateway("sk").charge(amount_minor=5000, currency="SAR", token="src", idempotency_key="k").ok
    assert seen["tap"][1]["amount"] == 50.0                  # Tap uses MAJOR units
    assert C.CheckoutGateway("sk").charge(amount_minor=5000, currency="SAR", token="tok", idempotency_key="k").ok
    assert seen["checkout"][1]["amount"] == 5000 and seen["checkout"][1]["source"]["token"] == "tok"


def test_null_gateway_fails_closed():
    from app.adapters.payment.null import NullGateway
    with pytest.raises(GatewayUnavailable):
        NullGateway().charge(amount_minor=100, currency="SAR", token="x", idempotency_key="k")


def test_settling_a_past_due_invoice_reactivates_the_subscription():
    """An enterprise invoice lapses to past_due, then a BANK TRANSFER settles it off-gateway.
    The subscription must turn back on — otherwise the invoice reads `paid` while the entitlement
    stays off, the exact invoice/entitlement split off-gateway settlement exists to close."""
    st = MemoryDataStore()
    b = _svc(st, MockGW(ok=False))
    _, sub = _plan_sub(b)
    run(b.run_cycle(sub.id))
    pay = run(PaymentStore(st).list_tenant("t1", 10))[0]
    pid = pay["id"]
    for _ in range(CFG["dunning_max_attempts"] + 1):
        p = run(PaymentStore(st).fetch(pid))
        if p.next_retry_at is None:
            break
        p.next_retry_at = 0
        run(PaymentStore(st).save(p))
        run(b.dunning_sweep())
    assert run(SubscriptionStore(st).fetch(sub.id)).status == "past_due"

    # the invoice the cycle raised
    inv = run(InvoiceStore(st).list_tenant("t1", 10))[0]
    res = run(b.settle(invoice_id=inv["id"], method="bank_transfer", issuer="alrajhi",
                       reference="TRX-9001", amount_minor=inv["total_minor"], paid_at=__import__("time").time()))
    assert res["invoice_status"] in ("paid", "settled")
    assert run(SubscriptionStore(st).fetch(sub.id)).status == "active", \
        "a bank transfer paid the invoice but the entitlement stayed off"


# ── trial + one-time period boundaries (escalation) ─────────────────────────────
def test_a_trial_ends_at_trial_end_not_a_full_interval():
    # trial_end used to be written and never read: current_period_end got a full interval, so a
    # 14-day trial silently granted 30 days. The period end must BE the trial end (Stripe/Paddle:
    # the first charge is raised then and the cycle moves the period forward a full interval).
    st = MemoryDataStore()
    b = _svc(st, MockGW(ok=True))
    plan = run(b.create_plan(name="Pro", trial_days=14,
                             prices=[{"interval": "month", "amount_minor": 10000,
                                      "currency": "SAR", "type": "flat"}]))
    sub = run(b.subscribe(tenant="t1", plan_id=plan.id, payment_token="pm_1"))
    assert sub.status == "trialing"
    assert sub.current_period_end == sub.trial_end          # ends when it says, not a month later
    assert sub.trial_end > 0


def test_a_one_time_purchase_has_no_period_end_and_never_lapses():
    # interval "once" used to get current_period_end = now + 0 = now, which the fail-closed resolver
    # then dropped the next day. A one-time pack has NO renewing period → zero end, never lapses.
    st = MemoryDataStore()
    b = _svc(st, MockGW(ok=True))
    plan = run(b.create_plan(name="Pack", prices=[{"interval": "once", "amount_minor": 5000,
                                                   "currency": "SAR", "type": "flat"}]))
    sub = run(b.subscribe(tenant="t1", plan_id=plan.id, interval="once", payment_token="pm_1"))
    assert sub.current_period_end == 0.0 and sub.status == "active"


def test_the_renewal_sweep_skips_a_one_time_purchase():
    # A zero period end reads as `0 <= now`, so without the interval guard the sweep would
    # re-invoice a one-time pack every cycle. It must never be due.
    st = MemoryDataStore()
    b = _svc(st, MockGW(ok=True))
    plan = run(b.create_plan(name="Pack", prices=[{"interval": "once", "amount_minor": 5000,
                                                   "currency": "SAR", "type": "flat"}]))
    once = run(b.subscribe(tenant="t1", plan_id=plan.id, interval="once", payment_token="pm_1"))
    due = run(SubscriptionStore(st).due(now=9_999_999_999.0))
    assert once.id not in [s.id for s in due]


# ── reconcile: recover a missed/unverified webhook by pulling the gateway status ──
class MockGWWithStatus(MockGW):
    """A gateway whose charge fails (a missed/declined webhook) but whose status GET reports a
    later outcome — the reconcile scenario."""
    def __init__(self, charge_ok=False, status="succeeded"):
        super().__init__(ok=charge_ok)
        self._status = status

    def fetch_status(self, *, provider_id, currency=""):
        return Charge(ok=self._status == "succeeded", status=self._status, provider="mock",
                      provider_id=provider_id)


def test_reconcile_applies_a_missed_success_through_the_same_seam_and_is_idempotent():
    st = MemoryDataStore()
    b = _svc(st, MockGWWithStatus(charge_ok=False, status="succeeded"))
    _, sub = _plan_sub(b)
    inv = run(b.run_cycle(sub.id))
    assert inv.status != "paid"                                  # the charge failed -> unpaid
    pay = run(b._payments.by_provider_id("ch_" + inv.id[:6]))
    assert pay is not None and pay.status == "failed"
    # the gateway now reports it succeeded (the webhook never arrived): reconcile applies it
    res = run(b.reconcile_payment(pay.id))
    assert res["ok"] and res["reconciled"] and res["payment_status"] == "succeeded"
    assert run(b._invoices.fetch(inv.id)).status == "paid"
    # idempotent — a second reconcile (or a late webhook) changes nothing
    again = run(b.reconcile_payment(pay.id))
    assert again["reconciled"] is False and again["payment_status"] == "succeeded"
    assert run(b._invoices.fetch(inv.id)).status == "paid"


def test_reconcile_is_a_no_op_while_the_gateway_still_shows_pending():
    st = MemoryDataStore()
    b = _svc(st, MockGWWithStatus(charge_ok=False, status="pending"))
    _, sub = _plan_sub(b)
    inv = run(b.run_cycle(sub.id))
    pay = run(b._payments.by_provider_id("ch_" + inv.id[:6]))
    res = run(b.reconcile_payment(pay.id))
    assert res["ok"] and res["reconciled"] is False and res["provider_status"] == "pending"
    assert run(b._invoices.fetch(inv.id)).status != "paid"      # nothing invented


def test_reconcile_reports_unsupported_when_the_gateway_has_no_status_endpoint():
    st = MemoryDataStore()
    b = _svc(st, MockGW(ok=False))                              # plain MockGW: no fetch_status
    _, sub = _plan_sub(b)
    inv = run(b.run_cycle(sub.id))
    pay = run(b._payments.by_provider_id("ch_" + inv.id[:6]))
    res = run(b.reconcile_payment(pay.id))
    assert res["ok"] is False and res["reason"] == "UNSUPPORTED"


def test_reconcile_of_an_unknown_payment_is_not_found():
    st = MemoryDataStore()
    b = _svc(st, MockGWWithStatus())
    assert run(b.reconcile_payment("pay_missing"))["reason"] == "NOT_FOUND"
