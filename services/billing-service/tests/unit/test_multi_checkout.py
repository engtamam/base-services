"""Multi-plan (cart) checkout — one invoice, one charge, N subscriptions, all-or-nothing.

Money-critical, so this leans on the SAME primitives a single subscribe+cycle uses and asserts the
hard guarantees: validate-all-before-any-write, exactly one charge, idempotent replay, and correct
per-item semantics (recurring / once / trial). Security + edge cases are first-class here.
"""
from __future__ import annotations

import pytest

from app.adapters.datastore.memory import MemoryDataStore
from app.core.stores import SubscriptionStore

from tests.unit.test_consolidated_cycle import _invoices, _svc, run


def _plan(b, name, amount=10000, *, interval="month", currency="SAR", trial_days=0, credits=0):
    prices = [{"interval": interval, "amount_minor": amount, "currency": currency, "type": "flat"}]
    limits = {"credits": credits} if credits else {}
    return run(b.create_plan(name=name, prices=prices, limits=limits, trial_days=trial_days))


def _subs(st):
    return run(st.scan_all("subscriptions", 100))


def _payments(st):
    return run(st.scan_all("payments", 100))


# ── happy path ─────────────────────────────────────────────────────────────────
def test_two_recurring_plans_one_invoice_one_charge_two_subs():
    st = MemoryDataStore(); b = _svc(st, "consolidated")
    a = _plan(b, "AppA", 10000); c = _plan(b, "AppB", 5000)
    res = run(b.checkout(tenant="t1", items=[{"plan_id": a.id}, {"plan_id": c.id}],
                         payment_token="pm"))
    assert res["paid"] is True
    assert res["total_minor"] == 17250                    # (10000+5000) + 15% VAT
    assert {i["plan_id"] for i in res["items"]} == {a.id, c.id}
    invs = _invoices(st)
    assert len(invs) == 1 and len(invs[0]["lines"]) == 2   # ONE invoice, a line per plan
    assert len(_payments(st)) == 1                          # ONE charge
    assert len(_subs(st)) == 2 and all(s["status"] == "active" for s in _subs(st))


def test_grand_total_never_sums_across_two_currencies():
    st = MemoryDataStore(); b = _svc(st, "consolidated")
    a = _plan(b, "SAR", 10000, currency="SAR"); c = _plan(b, "USD", 8000, currency="USD")
    with pytest.raises(ValueError, match="MIXED_CURRENCY_CART"):
        run(b.checkout(tenant="t1", items=[{"plan_id": a.id}, {"plan_id": c.id}], payment_token="pm"))
    assert _subs(st) == [] and _invoices(st) == []          # refused before any write


# ── validate-all-first: any bad item refuses the WHOLE cart, creating nothing ─────
def test_one_invalid_plan_refuses_the_whole_cart_nothing_created():
    st = MemoryDataStore(); b = _svc(st, "consolidated")
    a = _plan(b, "AppA", 10000)
    with pytest.raises(ValueError, match="PLAN_NOT_FOUND"):
        run(b.checkout(tenant="t1", items=[{"plan_id": a.id}, {"plan_id": "ghost"}], payment_token="pm"))
    assert _subs(st) == [] and _invoices(st) == [] and _payments(st) == []


def test_a_duplicate_plan_in_the_cart_is_refused():
    st = MemoryDataStore(); b = _svc(st, "consolidated")
    a = _plan(b, "AppA", 10000)
    with pytest.raises(ValueError, match="DUPLICATE_PLAN_IN_CART"):
        run(b.checkout(tenant="t1", items=[{"plan_id": a.id}, {"plan_id": a.id}], payment_token="pm"))
    assert _subs(st) == []


def test_an_already_held_plan_is_refused():
    st = MemoryDataStore(); b = _svc(st, "consolidated")
    a = _plan(b, "AppA", 10000); c = _plan(b, "AppB", 5000)
    run(b.subscribe(tenant="t1", plan_id=a.id, payment_token="pm"))     # already subscribed to A
    with pytest.raises(ValueError, match="ALREADY_SUBSCRIBED"):
        run(b.checkout(tenant="t1", items=[{"plan_id": a.id}, {"plan_id": c.id}], payment_token="pm"))
    assert len(_subs(st)) == 1                              # only the pre-existing A; cart created nothing


def test_an_inactive_plan_is_refused():
    st = MemoryDataStore(); b = _svc(st, "consolidated")
    a = _plan(b, "AppA", 10000); c = _plan(b, "AppB", 5000)
    gone = run(b._plans.fetch(c.id)); gone.active = False; run(b._plans.create(gone))
    with pytest.raises(ValueError, match="PLAN_NOT_AVAILABLE"):
        run(b.checkout(tenant="t1", items=[{"plan_id": a.id}, {"plan_id": c.id}], payment_token="pm"))
    assert _subs(st) == []


def test_empty_cart_is_refused():
    st = MemoryDataStore(); b = _svc(st, "consolidated")
    with pytest.raises(ValueError, match="EMPTY_CART"):
        run(b.checkout(tenant="t1", items=[], payment_token="pm"))


def test_a_bad_coupon_refuses_the_whole_cart_before_redeeming():
    st = MemoryDataStore(); b = _svc(st, "consolidated")
    a = _plan(b, "AppA", 10000); c = _plan(b, "AppB", 5000)
    run(b.upsert_coupon(code="AONLY", type="percent", value=50, plan_id=a.id))
    # AONLY is valid only for plan A; attaching it to C must refuse the whole cart, redeeming nothing.
    with pytest.raises(ValueError, match="COUPON_PLAN_MISMATCH"):
        run(b.checkout(tenant="t1", items=[{"plan_id": a.id, "coupon_code": "AONLY"},
                                           {"plan_id": c.id, "coupon_code": "AONLY"}], payment_token="pm"))
    assert _subs(st) == []
    assert run(b._coupons.fetch("AONLY")).redeemed == 0     # nothing redeemed on a refused cart


# ── money safety: idempotency + charge decline ───────────────────────────────────
def test_idempotency_key_replays_and_never_double_charges():
    st = MemoryDataStore(); b = _svc(st, "consolidated")
    a = _plan(b, "AppA", 10000); c = _plan(b, "AppB", 5000)
    args = dict(tenant="t1", items=[{"plan_id": a.id}, {"plan_id": c.id}], payment_token="pm",
                idempotency_key="cart-1")
    first = run(b.checkout(**args))
    second = run(b.checkout(**args))                        # a retry with the same key
    assert first == second                                  # replayed, identical result
    assert len(_invoices(st)) == 1 and len(_payments(st)) == 1 and len(_subs(st)) == 2


def test_a_declined_charge_is_all_or_nothing_one_invoice_in_dunning_no_partial():
    from tests.unit.test_consolidated_cycle import MockGW

    class _Decline(MockGW):
        def charge(self, **k):
            from app.core.stores import Payment  # noqa
            from app.ports.payment_gateway import Charge
            return Charge(ok=False, provider="mock", provider_id="ch_x", status="declined",
                          amount_minor=k.get("amount_minor", 0), currency=k.get("currency", "SAR"),
                          error="declined")

    st = MemoryDataStore()
    b = _svc(st, "consolidated")
    b._gw = _Decline()
    a = _plan(b, "AppA", 10000); c = _plan(b, "AppB", 5000)
    res = run(b.checkout(tenant="t1", items=[{"plan_id": a.id}, {"plan_id": c.id}], payment_token="pm"))
    assert res["paid"] is False
    invs = _invoices(st)
    assert len(invs) == 1 and invs[0]["status"] != "paid"   # ONE invoice, unpaid (dunning), no partial
    pays = _payments(st)
    assert len(pays) == 1 and pays[0]["status"] == "failed"  # ONE charge attempt, failed — not 2


# ── edge cases: once, trial, single item ─────────────────────────────────────────
def test_mixed_once_and_recurring_bill_together_once_never_renews():
    st = MemoryDataStore(); b = _svc(st, "consolidated")
    rec = _plan(b, "Monthly", 10000, interval="month")
    once = _plan(b, "OneOff", 3000, interval="once")
    res = run(b.checkout(tenant="t1", items=[{"plan_id": rec.id},
                                             {"plan_id": once.id, "interval": "once"}],
                         payment_token="pm"))
    assert res["paid"] is True and len(_invoices(st)) == 1 and len(_payments(st)) == 1
    by_plan = {s["plan_id"]: s for s in _subs(st)}
    assert float(by_plan[once.id]["current_period_end"]) == 0.0     # once never renews (sweep skips)
    assert float(by_plan[rec.id]["current_period_end"]) > 0.0       # recurring advanced


def test_a_trial_item_is_created_but_not_charged_now():
    st = MemoryDataStore(); b = _svc(st, "consolidated")
    paid_plan = _plan(b, "Paid", 10000)
    trial_plan = _plan(b, "Trial", 20000, trial_days=14)
    res = run(b.checkout(tenant="t1", items=[{"plan_id": paid_plan.id}, {"plan_id": trial_plan.id}],
                         payment_token="pm"))
    by_plan = {i["plan_id"]: i for i in res["items"]}
    assert by_plan[trial_plan.id]["status"] == "trialing"
    assert by_plan[paid_plan.id]["status"] == "active"
    # only the paid plan is on the invoice; the trial is not charged now
    assert _invoices(st)[0]["total_minor"] == 11500                 # 10000 + VAT, trial excluded
    assert len(_payments(st)) == 1


def test_an_all_trial_cart_charges_nothing_and_creates_no_invoice():
    st = MemoryDataStore(); b = _svc(st, "consolidated")
    t1 = _plan(b, "T1", 10000, trial_days=14); t2 = _plan(b, "T2", 5000, trial_days=7)
    res = run(b.checkout(tenant="t1", items=[{"plan_id": t1.id}, {"plan_id": t2.id}], payment_token="pm"))
    assert res["paid"] is True and res["invoice_id"] == "" and res["total_minor"] == 0
    assert _invoices(st) == [] and _payments(st) == []
    assert all(s["status"] == "trialing" for s in _subs(st))


def test_a_coupon_discount_applies_per_item_in_the_cart():
    st = MemoryDataStore(); b = _svc(st, "consolidated")
    run(b.upsert_coupon(code="HALF", type="percent", value=50))
    a = _plan(b, "AppA", 10000); c = _plan(b, "AppB", 5000)
    run(b.checkout(tenant="t1", items=[{"plan_id": a.id, "coupon_code": "HALF"}, {"plan_id": c.id}],
                   payment_token="pm"))
    # A halved (10000 -> 5000 net) + B full 5000 = 10000 net + 15% VAT
    assert _invoices(st)[0]["total_minor"] == 11500


# ── the "unified renewal" answer: aligned subs renew on ONE invoice ──────────────
def test_cart_items_renew_together_on_one_consolidated_invoice():
    st = MemoryDataStore(); b = _svc(st, "consolidated")
    a = _plan(b, "AppA", 10000); c = _plan(b, "AppB", 5000)
    run(b.checkout(tenant="t1", items=[{"plan_id": a.id}, {"plan_id": c.id}], payment_token="pm"))
    # force both due at their shared grid boundary
    from app.core.models import now_ts
    ended = now_ts() - 10.0
    subs = SubscriptionStore(st)
    for s in _subs(st):
        row = run(subs._store.get("subscriptions", s["id"]))
        row["current_period_end"] = ended
        run(subs._store.put("subscriptions", s["id"], row))
    before = len(_invoices(st))
    run(b.cycle_due_sweep())                               # consolidated mode → ONE renewal invoice
    assert len(_invoices(st)) == before + 1                # a single unified renewal invoice for both


# ── quantity / pricing detail ────────────────────────────────────────────────────
def test_per_seat_quantity_is_billed_across_the_cart():
    st = MemoryDataStore(); b = _svc(st, "consolidated")
    seat = run(b.create_plan(name="Seats", prices=[{"interval": "month", "amount_minor": 5000,
               "currency": "SAR", "type": "per_seat"}]))
    flat = _plan(b, "Flat", 10000)
    run(b.checkout(tenant="t1", items=[{"plan_id": seat.id, "quantity": 3}, {"plan_id": flat.id}],
                   payment_token="pm"))
    assert _invoices(st)[0]["total_minor"] == 28750          # (5000*3 + 10000) + 15% VAT


def test_a_coupon_is_redeemed_exactly_once_per_cart():
    st = MemoryDataStore(); b = _svc(st, "consolidated")
    run(b.upsert_coupon(code="ONE", type="percent", value=25, max_redemptions=1))
    a = _plan(b, "AppA", 10000); c = _plan(b, "AppB", 5000)
    run(b.checkout(tenant="t1", items=[{"plan_id": a.id, "coupon_code": "ONE"}, {"plan_id": c.id}],
                   payment_token="pm"))
    assert run(b._coupons.fetch("ONE")).redeemed == 1


def test_a_year_interval_item_is_priced_at_its_year_price():
    st = MemoryDataStore(); b = _svc(st, "consolidated")
    yr = run(b.create_plan(name="Annual", prices=[
        {"interval": "month", "amount_minor": 10000, "currency": "SAR", "type": "flat"},
        {"interval": "year", "amount_minor": 100000, "currency": "SAR", "type": "flat"}]))
    run(b.checkout(tenant="t1", items=[{"plan_id": yr.id, "interval": "year"}], payment_token="pm"))
    assert _invoices(st)[0]["total_minor"] == 115000         # 100000 + 15% VAT


def test_a_plan_without_the_requested_interval_refuses_the_cart():
    st = MemoryDataStore(); b = _svc(st, "consolidated")
    m = _plan(b, "MonthOnly", 10000, interval="month")
    with pytest.raises(ValueError, match="PLAN_INTERVAL_UNAVAILABLE"):
        run(b.checkout(tenant="t1", items=[{"plan_id": m.id, "interval": "year"}], payment_token="pm"))
    assert _subs(st) == []


# ── more validation edges ────────────────────────────────────────────────────────
def test_empty_plan_id_is_refused():
    st = MemoryDataStore(); b = _svc(st, "consolidated")
    with pytest.raises(ValueError, match="PLAN_ID_REQUIRED"):
        run(b.checkout(tenant="t1", items=[{"plan_id": ""}], payment_token="pm"))


def test_zero_quantity_is_refused_and_creates_nothing():
    st = MemoryDataStore(); b = _svc(st, "consolidated")
    a = _plan(b, "AppA", 10000)
    with pytest.raises(ValueError, match="INVALID_QUANTITY"):
        run(b.checkout(tenant="t1", items=[{"plan_id": a.id, "quantity": 0}], payment_token="pm"))
    assert _subs(st) == []


def test_a_five_plan_cart_is_one_invoice_with_five_lines():
    st = MemoryDataStore(); b = _svc(st, "consolidated")
    ids = [_plan(b, f"App{i}", 1000 * (i + 1)).id for i in range(5)]
    run(b.checkout(tenant="t1", items=[{"plan_id": i} for i in ids], payment_token="pm"))
    invs = _invoices(st)
    assert len(invs) == 1 and len(invs[0]["lines"]) == 5 and len(_subs(st)) == 5


# ── idempotency edges ────────────────────────────────────────────────────────────
def test_distinct_idempotency_keys_are_distinct_checkouts():
    st = MemoryDataStore(); b = _svc(st, "consolidated")
    a = _plan(b, "AppA", 10000); c = _plan(b, "AppB", 5000)
    run(b.checkout(tenant="t1", items=[{"plan_id": a.id}], payment_token="pm", idempotency_key="k1"))
    run(b.checkout(tenant="t1", items=[{"plan_id": c.id}], payment_token="pm", idempotency_key="k2"))
    assert len(_invoices(st)) == 2 and len(_subs(st)) == 2


def test_idempotency_key_is_tenant_scoped():
    st = MemoryDataStore(); b = _svc(st, "consolidated")
    a = _plan(b, "AppA", 10000)
    r1 = run(b.checkout(tenant="t1", items=[{"plan_id": a.id}], payment_token="pm", idempotency_key="same"))
    r2 = run(b.checkout(tenant="t2", items=[{"plan_id": a.id}], payment_token="pm", idempotency_key="same"))
    assert r1["tenant"] == "t1" and r2["tenant"] == "t2"
    assert r1["items"][0]["subscription_id"] != r2["items"][0]["subscription_id"]


def test_a_pending_claim_reports_in_progress_not_a_second_charge():
    st = MemoryDataStore(); b = _svc(st, "consolidated")
    a = _plan(b, "AppA", 10000)
    from app.core.models import now_ts
    ckey = f"checkout:{len('t1')}:t1|busy"
    run(b._idempo._store.cas("idempo", ckey, 0,
                             {"key": ckey, "status": "pending", "expires_at": now_ts() + 120}))
    with pytest.raises(ValueError, match="CHECKOUT_IN_PROGRESS"):
        run(b.checkout(tenant="t1", items=[{"plan_id": a.id}], payment_token="pm", idempotency_key="busy"))
    assert _payments(st) == []


# ── tenant isolation ─────────────────────────────────────────────────────────────
def test_one_tenants_cart_never_touches_another_tenant():
    st = MemoryDataStore(); b = _svc(st, "consolidated")
    a = _plan(b, "AppA", 10000)
    run(b.checkout(tenant="t1", items=[{"plan_id": a.id}], payment_token="pm"))
    run(b.checkout(tenant="t2", items=[{"plan_id": a.id}], payment_token="pm"))   # t1 holding it must not block t2
    assert {s["tenant"] for s in _subs(st)} == {"t1", "t2"} and len(_subs(st)) == 2


# ── events ───────────────────────────────────────────────────────────────────────
def test_a_renewed_event_is_emitted_per_recurring_item_only():
    from app.core import events as ev
    from app.core.events import Emitter

    class _Bus:
        def __init__(self):
            self.events = []

        async def publish(self, topic, event):
            self.events.append(event)

    st = MemoryDataStore(); b = _svc(st, "consolidated")
    b._events = Emitter(_Bus())
    rec = _plan(b, "Rec", 10000, interval="month")
    once = _plan(b, "Once", 3000, interval="once")
    trial = _plan(b, "Trial", 5000, trial_days=14)
    run(b.checkout(tenant="t1", items=[{"plan_id": rec.id}, {"plan_id": once.id, "interval": "once"},
                                       {"plan_id": trial.id}], payment_token="pm"))
    renewed = [e for e in b._events._bus.events if e.type == ev.SUBSCRIPTION_RENEWED]
    assert len(renewed) == 1 and renewed[0].payload["plan_id"] == rec.id


# ══ checkout_preview: read-only pre-purchase pricing ═════════════════════════════
def _preview(b, tenant, items, **kw):
    return run(b.checkout_preview(tenant=tenant, items=items, **kw))


def test_preview_has_no_side_effects():
    st = MemoryDataStore(); b = _svc(st, "consolidated")
    a = _plan(b, "A", 10000); c = _plan(b, "B", 5000)
    _preview(b, "t1", [{"plan_id": a.id}, {"plan_id": c.id}])
    assert _subs(st) == [] and _invoices(st) == [] and _payments(st) == []


def test_preview_totals_and_per_item_breakdown():
    st = MemoryDataStore(); b = _svc(st, "consolidated")
    run(b.upsert_coupon(code="HALF", type="percent", value=50))
    a = _plan(b, "A", 10000); c = _plan(b, "B", 5000)
    p = _preview(b, "t1", [{"plan_id": a.id, "coupon_code": "HALF"}, {"plan_id": c.id}])
    assert p["currency"] == "SAR"
    assert p["subtotal_minor"] == 15000 and p["discount_minor"] == 5000
    assert p["tax_minor"] == 1500 and p["total_minor"] == 11500      # VAT on the DISCOUNTED net
    # per-item sums reconcile to the aggregate
    assert sum(i["total_minor"] for i in p["items"]) == p["total_minor"]
    assert sum(i["discount_minor"] for i in p["items"]) == p["discount_minor"]
    assert sum(i["tax_minor"] for i in p["items"]) == p["tax_minor"]


def test_preview_equals_what_checkout_actually_charges():
    st = MemoryDataStore(); b = _svc(st, "consolidated")
    run(b.upsert_coupon(code="HALF", type="percent", value=50))
    a = _plan(b, "A", 10000); c = _plan(b, "B", 5000)
    items = [{"plan_id": a.id, "coupon_code": "HALF"}, {"plan_id": c.id}]
    pre = _preview(b, "t1", items)                          # preview FIRST (no side effects)
    res = run(b.checkout(tenant="t1", items=items, payment_token="pm"))  # then really charge
    inv = _invoices(st)[0]
    assert pre["total_minor"] == inv["total_minor"] == res["total_minor"]  # preview == the real bill
    assert pre["discount_minor"] == inv["discount_minor"]
    assert pre["tax_minor"] == inv["tax_minor"]


def test_preview_matches_a_prorated_first_period_exactly():
    """The alignment-accuracy case: with an existing same-interval sub, a new item's first period is
    PRORATED — the preview must show the prorated amount, identical to what checkout charges."""
    from app.core.models import now_ts
    st = MemoryDataStore(); b = _svc(st, "consolidated")
    grid = _plan(b, "Grid", 10000); new = _plan(b, "New", 30000)
    run(b.subscribe(tenant="t1", plan_id=grid.id, payment_token="pm"))   # sets the tenant's grid
    subs = SubscriptionStore(st)
    row = run(subs._store.get("subscriptions", _subs(st)[0]["id"]))
    row["current_period_end"] = now_ts() + 10 * 86400       # grid ends in ~10 days → new item prorates
    run(subs._store.put("subscriptions", row["id"], row))

    pre = _preview(b, "t1", [{"plan_id": new.id}])
    assert pre["items"][0]["prorated"] is True
    res = run(b.checkout(tenant="t1", items=[{"plan_id": new.id}], payment_token="pm"))
    inv = _invoices(st)[0]
    assert pre["total_minor"] == inv["total_minor"] == res["total_minor"]  # prorated, to the minor unit
    assert pre["total_minor"] < 34500                       # strictly less than a full 30000+VAT period


def test_preview_within_cart_alignment_matches_checkout():
    st = MemoryDataStore(); b = _svc(st, "consolidated")
    a = _plan(b, "A", 10000); c = _plan(b, "B", 6000)
    items = [{"plan_id": a.id}, {"plan_id": c.id}]           # 2nd aligns to the 1st (same instant → equal)
    pre = _preview(b, "t1", items)
    run(b.checkout(tenant="t1", items=items, payment_token="pm"))
    assert pre["total_minor"] == _invoices(st)[0]["total_minor"]


def test_preview_tax_is_zero_when_vat_is_off():
    st = MemoryDataStore()
    from tests.unit.test_consolidated_cycle import _svc as _mk
    b = _mk(st, "consolidated")
    b._cfg = dict(b._cfg, vat_rate="0")
    b._tax = __import__("app.adapters.tax.vat", fromlist=["VatCalculator"]).VatCalculator("0")
    a = _plan(b, "A", 10000)
    p = _preview(b, "t1", [{"plan_id": a.id}])
    assert p["tax_minor"] == 0 and p["total_minor"] == 10000  # no VAT → total is the net


def test_preview_reverse_charge_buyer_pays_no_vat():
    st = MemoryDataStore(); b = _svc(st, "consolidated")
    b._cfg = dict(b._cfg, reverse_charge=True)
    a = _plan(b, "A", 10000)
    p = _preview(b, "t1", [{"plan_id": a.id}], buyer_vat="300000000000003")
    assert p["tax_minor"] == 0 and p["total_minor"] == 10000  # VAT-registered buyer accounts for VAT


def test_preview_trial_item_is_not_charged_now():
    st = MemoryDataStore(); b = _svc(st, "consolidated")
    paid = _plan(b, "Paid", 10000); trial = _plan(b, "Trial", 20000, trial_days=14)
    p = _preview(b, "t1", [{"plan_id": paid.id}, {"plan_id": trial.id}])
    by = {i["plan_id"]: i for i in p["items"]}
    assert by[trial.id]["charged_now"] is False and by[trial.id]["total_minor"] == 0
    assert p["total_minor"] == 11500                        # only the paid plan is due now


def test_preview_once_item_priced_now():
    st = MemoryDataStore(); b = _svc(st, "consolidated")
    once = _plan(b, "Once", 3000, interval="once")
    p = _preview(b, "t1", [{"plan_id": once.id, "interval": "once"}])
    assert p["items"][0]["charged_now"] is True and p["total_minor"] == 3450  # 3000 + VAT


def test_preview_quantity_and_year_interval():
    st = MemoryDataStore(); b = _svc(st, "consolidated")
    seat = run(b.create_plan(name="Seats", prices=[{"interval": "month", "amount_minor": 5000,
               "currency": "SAR", "type": "per_seat"}]))
    yr = run(b.create_plan(name="Yr", prices=[{"interval": "year", "amount_minor": 100000,
             "currency": "SAR", "type": "flat"}]))
    p = _preview(b, "t1", [{"plan_id": seat.id, "quantity": 3}])
    assert p["total_minor"] == 17250                        # 5000*3 + VAT
    p2 = _preview(b, "t1", [{"plan_id": yr.id, "interval": "year"}])
    assert p2["total_minor"] == 115000


# ── preview refuses exactly what checkout refuses, creating nothing ──────────────
def test_preview_refuses_mixed_currency():
    st = MemoryDataStore(); b = _svc(st, "consolidated")
    a = _plan(b, "SAR", 10000, currency="SAR"); c = _plan(b, "USD", 8000, currency="USD")
    with pytest.raises(ValueError, match="MIXED_CURRENCY_CART"):
        _preview(b, "t1", [{"plan_id": a.id}, {"plan_id": c.id}])
    assert _subs(st) == []


def test_preview_refuses_bad_duplicate_held_and_empty():
    st = MemoryDataStore(); b = _svc(st, "consolidated")
    a = _plan(b, "A", 10000)
    with pytest.raises(ValueError, match="PLAN_NOT_FOUND"):
        _preview(b, "t1", [{"plan_id": "ghost"}])
    with pytest.raises(ValueError, match="DUPLICATE_PLAN_IN_CART"):
        _preview(b, "t1", [{"plan_id": a.id}, {"plan_id": a.id}])
    with pytest.raises(ValueError, match="EMPTY_CART"):
        _preview(b, "t1", [])
    run(b.subscribe(tenant="t1", plan_id=a.id, payment_token="pm"))
    with pytest.raises(ValueError, match="ALREADY_SUBSCRIBED"):
        _preview(b, "t1", [{"plan_id": a.id}])


# ── no idempotency_key: the charge is STILL protected (auto-derived key) ──────────
def test_checkout_without_a_key_still_creates_exactly_one_cart():
    st = MemoryDataStore(); b = _svc(st, "consolidated")
    a = _plan(b, "A", 10000); c = _plan(b, "B", 5000)
    run(b.checkout(tenant="t1", items=[{"plan_id": a.id}, {"plan_id": c.id}], payment_token="pm"))
    assert len(_invoices(st)) == 1 and len(_payments(st)) == 1


def test_checkout_without_a_key_is_guarded_against_a_concurrent_double_charge():
    """The HIGH from the audit: with no client idempotency_key the charge must still be covered. A
    concurrent checkout of the same cart holds the auto-derived claim, so the second is refused BEFORE
    it can charge — no second invoice, no second charge."""
    import hashlib
    from app.core.models import now_ts
    st = MemoryDataStore(); b = _svc(st, "consolidated")
    a = _plan(b, "A", 10000)
    digest = hashlib.sha256(repr([(a.id, "month", 1, "")]).encode()).hexdigest()[:32]
    ckey = f"checkout:{len('t1')}:t1|auto:{digest}"
    run(b._idempo._store.cas("idempo", ckey, 0,
                             {"key": ckey, "status": "pending", "expires_at": now_ts() + 120}))
    with pytest.raises(ValueError, match="CHECKOUT_IN_PROGRESS"):
        run(b.checkout(tenant="t1", items=[{"plan_id": a.id}], payment_token="pm"))  # NO idempotency_key
    assert _payments(st) == [] and _invoices(st) == []   # nothing charged while another holds it


def test_checkout_without_a_key_sequential_duplicate_never_double_charges():
    st = MemoryDataStore(); b = _svc(st, "consolidated")
    a = _plan(b, "A", 10000)
    run(b.checkout(tenant="t1", items=[{"plan_id": a.id}], payment_token="pm"))
    with pytest.raises(ValueError, match="ALREADY_SUBSCRIBED"):
        run(b.checkout(tenant="t1", items=[{"plan_id": a.id}], payment_token="pm"))
    assert len(_invoices(st)) == 1 and len(_payments(st)) == 1   # still exactly one charge
