"""GET /subscriptions/renewal-preview — an honest, read-only preview of the next renewal total.

The guarantee: the preview reuses the SAME _compute_renewal the real consolidated invoice cut uses
(best of coupon vs cross-app rule, never summed), groups exactly as consolidated billing does (one
group per gateway+currency, never summing two currencies), and mutates NOTHING.
"""
from __future__ import annotations

from app.adapters.datastore.memory import MemoryDataStore
from app.core.stores import InvoiceStore, SubscriptionStore

from tests.unit.test_consolidated_cycle import _invoices, _svc, _two_due_subs, run


def _sar_plan(b, name="A", amount=10000):
    return run(b.create_plan(name=name, prices=[{"interval": "month", "amount_minor": amount,
               "currency": "SAR", "type": "flat"}], limits={"credits": 10}))


# ── one source of truth: the preview equals what the cycle actually bills ───────────
def test_preview_equals_what_the_consolidated_cycle_actually_bills():
    st = MemoryDataStore(); b = _svc(st, "consolidated")
    _two_due_subs(b, st)                                   # two active SAR subs, 10000 + 5000
    pre = run(b.renewal_preview("t1"))
    assert len(pre["groups"]) == 1
    g = pre["groups"][0]
    assert g["currency"] == "SAR" and len(g["lines"]) == 2
    assert g["total_minor"] == 17250                      # (10000+5000) + 15% VAT
    # Now actually bill and confirm the previewed number was honest.
    run(b.cycle_due_sweep())
    inv = _invoices(st)[0]
    assert inv["total_minor"] == g["total_minor"]


def test_preview_has_no_side_effects():
    st = MemoryDataStore(); b = _svc(st, "consolidated")
    _two_due_subs(b, st)
    run(b.renewal_preview("t1"))
    assert run(st.scan_all("invoices", 100)) == []        # no invoice cut
    assert run(st.scan_all("payments", 100)) == []        # no charge
    assert run(st.scan_all("ledger", 100)) == []          # nothing recorded


# ── grouping: never sum two currencies ─────────────────────────────────────────────
def test_two_currencies_are_two_groups_never_summed():
    st = MemoryDataStore(); b = _svc(st, "consolidated")
    p_sar = _sar_plan(b, "SarApp", 10000)
    p_usd = run(b.create_plan(name="UsdApp", prices=[{"interval": "month", "amount_minor": 8000,
                "currency": "USD", "type": "flat"}], limits={"credits": 10}))
    run(b.subscribe(tenant="t1", plan_id=p_sar.id, payment_token="pm"))
    run(b.subscribe(tenant="t1", plan_id=p_usd.id, payment_token="pm"))
    groups = run(b.renewal_preview("t1"))["groups"]
    assert {g["currency"] for g in groups} == {"SAR", "USD"}
    for g in groups:
        assert len(g["lines"]) == 1                       # each currency stands alone, never merged


# ── selection: only live renewing subs ─────────────────────────────────────────────
def test_excludes_cancelling_once_and_trialing_subscriptions():
    st = MemoryDataStore(); b = _svc(st, "consolidated")
    p = _sar_plan(b)
    keep = run(b.subscribe(tenant="t1", plan_id=p.id, payment_token="pm"))
    cancelling = run(b.subscribe(tenant="t1", plan_id=p.id, payment_token="pm"))
    trialing = run(b.subscribe(tenant="t1", plan_id=p.id, payment_token="pm"))
    onceoff = run(b.subscribe(tenant="t1", plan_id=p.id, payment_token="pm"))
    subs = SubscriptionStore(st)
    for sid, patch in [(cancelling.id, {"cancel_at_period_end": True}),
                       (trialing.id, {"status": "trialing"}),
                       (onceoff.id, {"interval": "once"})]:
        row = run(subs._store.get("subscriptions", sid)); row.update(patch)
        run(subs._store.put("subscriptions", sid, row))
    ids = {ln["subscription_id"] for g in run(b.renewal_preview("t1"))["groups"] for ln in g["lines"]}
    assert ids == {keep.id}                               # only the clean active renewing sub


def test_empty_when_the_tenant_has_no_renewing_subscriptions():
    st = MemoryDataStore(); b = _svc(st, "consolidated")
    assert run(b.renewal_preview("nobody"))["groups"] == []


# ── discounts: the preview shows the real coupon discount ──────────────────────────
def test_preview_reflects_a_coupon_discount_the_same_way_the_invoice_would():
    st = MemoryDataStore(); b = _svc(st, "consolidated")
    run(b.upsert_coupon(code="HALF", type="percent", value=50))
    p = _sar_plan(b, "A", 10000)
    run(b.subscribe(tenant="t1", plan_id=p.id, payment_token="pm", coupon_code="HALF"))
    line = run(b.renewal_preview("t1"))["groups"][0]["lines"][0]
    assert line["subtotal_minor"] == 10000
    assert line["discount_minor"] == 5000                 # 50% off
    assert line["total_minor"] == 5750                    # 5000 net + 15% VAT
    assert line["plan_name"] == "A"


# ── STRONG COUPLING: whatever changes the price, the preview equals the real bill ──
def _force_due(st, *sub_ids):
    from app.core.models import now_ts
    ended = now_ts() - 10.0
    subs = SubscriptionStore(st)
    for sid in sub_ids:
        row = run(subs._store.get("subscriptions", sid))
        row["status"] = "active"; row["current_period_end"] = ended
        row["first_period_prorated"] = False
        run(subs._store.put("subscriptions", sid, row))


def test_preview_equals_the_invoice_across_grandfathering_downgrade_coupon_and_quantity():
    """Every price-affecting path at once — grandfathered price, a SCHEDULED downgrade, a coupon, and a
    per-seat quantity — priced through the SAME _compute_renewal, so the previewed group total is the
    exact number the consolidated invoice cuts. If any pricing path ever diverged, this fails."""
    st = MemoryDataStore(); b = _svc(st, "consolidated")
    run(b.upsert_coupon(code="TENOFF", type="percent", value=10))

    # (a) grandfathered: subscribe at 10000, then the operator raises the list price to 99999.
    p_gf = _sar_plan(b, "Grandfathered", 10000)
    s_gf = run(b.subscribe(tenant="t1", plan_id=p_gf.id, payment_token="pm"))
    gf = run(b._plans.fetch(p_gf.id)); gf.prices = [{"interval": "month", "amount_minor": 99999,
             "currency": "SAR", "type": "flat"}]; run(b._plans.create(gf))

    # (b) scheduled downgrade: on Pro (30000), change to Basic (10000) → adopted at renewal.
    pro = _sar_plan(b, "Pro", 30000)
    basic = _sar_plan(b, "Basic", 10000)
    s_dn = run(b.subscribe(tenant="t1", plan_id=pro.id, payment_token="pm"))
    run(b.change_plan(s_dn.id, new_plan_id=basic.id))     # new < old → scheduled downgrade

    # (c) coupon: 10% off a 20000 plan.
    p_cp = _sar_plan(b, "Couponed", 20000)
    s_cp = run(b.subscribe(tenant="t1", plan_id=p_cp.id, payment_token="pm", coupon_code="TENOFF"))

    # (d) per-seat quantity: 5000/seat × 3 seats.
    p_seat = run(b.create_plan(name="Seats", prices=[{"interval": "month", "amount_minor": 5000,
                 "currency": "SAR", "type": "per_seat"}], limits={"credits": 10}))
    s_seat = run(b.subscribe(tenant="t1", plan_id=p_seat.id, payment_token="pm", quantity=3))

    _force_due(st, s_gf.id, s_dn.id, s_cp.id, s_seat.id)

    pre = run(b.renewal_preview("t1"))
    assert len(pre["groups"]) == 1
    g = pre["groups"][0]
    # per-line honesty: grandfathered price kept, downgrade adopted, coupon applied, quantity multiplied
    by_sub = {ln["subscription_id"]: ln for ln in g["lines"]}
    assert by_sub[s_gf.id]["subtotal_minor"] == 10000                    # NOT the raised 99999
    assert by_sub[s_dn.id]["subtotal_minor"] == 10000 and by_sub[s_dn.id]["plan_name"] == "Basic"
    assert by_sub[s_cp.id]["discount_minor"] == 2000                     # 10% of 20000
    assert by_sub[s_seat.id]["subtotal_minor"] == 15000                  # 5000 × 3

    # THE COUPLING: the previewed group total is exactly what the real cycle bills.
    run(b.cycle_due_sweep())
    inv = _invoices(st)[0]
    assert inv["total_minor"] == g["total_minor"]
    assert len(inv["lines"]) == len(g["lines"]) == 4


def test_a_coupon_expiring_before_the_renewal_is_not_overstated():
    """Edge case: a coupon valid TODAY but expired by the renewal instant must NOT show a discount —
    the preview judges the coupon window at the renewal time (period end), matching what will bill.
    This is the divergence the as_of fix closes: judged at NOW the coupon still applies; judged at the
    renewal it does not."""
    st = MemoryDataStore(); b = _svc(st, "consolidated")
    from app.core.models import now_ts
    run(b.upsert_coupon(code="SOON", type="percent", value=50,
                        expires_at=now_ts() + 100))       # valid NOW, so subscribe accepts it
    p = _sar_plan(b, "A", 10000)
    s = run(b.subscribe(tenant="t1", plan_id=p.id, payment_token="pm", coupon_code="SOON"))
    subs = SubscriptionStore(st)
    row = run(subs._store.get("subscriptions", s.id))
    row["status"] = "active"; row["first_period_prorated"] = False
    row["current_period_end"] = now_ts() + 100_000        # renewal is AFTER the coupon expires
    run(subs._store.put("subscriptions", s.id, row))

    line = run(b.renewal_preview("t1"))["groups"][0]["lines"][0]
    assert line["discount_minor"] == 0                    # expired by renewal → not applied, not faked
    assert line["total_minor"] == 11500                   # 10000 + 15% VAT, no discount

    # Contrast: judged as-of NOW (no as_of) the coupon WOULD still discount — proving as_of is load-bearing.
    now_priced = run(b._compute_renewal(run(subs.fetch(s.id))))
    assert now_priced["discount"] == 5000
