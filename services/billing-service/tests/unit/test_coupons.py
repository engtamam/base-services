"""Coupons.

Three fields on this model were decorative and all three cost money: `max_redemptions` was never
enforced because nothing incremented `redeemed`; `expires_at` was never checked; and neither could
be set through the API anyway, so a panel offering "expires on" and "turn off" was offering
something the service could not do.

The tests that matter are therefore the refusals — a coupon that should have stopped discounting
and did.
"""
from __future__ import annotations

import asyncio

import pytest

from app.adapters.datastore.memory import MemoryDataStore
from app.adapters.pdf.null import NullPdf
from app.adapters.tax.vat import VatCalculator
from app.core import coupons as C
from app.core import pricing
from app.core.billing_service import BillingService
from app.core.stores import (CouponStore, IdempotencyStore, InvoiceStore, LedgerStore,
                             PaymentStore, PlanStore, SettlementStore, SubscriptionStore,
                             UsageStore)

from tests.unit.test_billing_core import CFG, MockEInv

NOW = 1_000_000.0


def run(c):
    return asyncio.run(c)


class _Gw:
    driver = "ok"

    def charge(self, **kw):
        from app.ports.payment_gateway import Charge
        return Charge(ok=True, provider_id="ch_1", status="succeeded")

    def refund(self, **kw):
        from app.ports.payment_gateway import Charge
        return Charge(ok=True, provider_id="rf_1", status="refunded")

    def verify_webhook(self, **kw):
        raise NotImplementedError


def _svc(st, now=NOW):
    return BillingService(
        plans=PlanStore(st), coupons=CouponStore(st), subs=SubscriptionStore(st),
        invoices=InvoiceStore(st), payments=PaymentStore(st), usage=UsageStore(st),
        ledger=LedgerStore(st), idempo=IdempotencyStore(st), settlements=SettlementStore(st),
        gateway=_Gw(), tax=VatCalculator("0.15"), einvoicer=MockEInv(), pdf=NullPdf(),
        config=CFG, clock=lambda: now)


def _plan(b):
    return run(b.create_plan(name="Pro", prices=[{"interval": "month", "amount_minor": 10000,
                                                  "currency": "SAR", "type": "flat"}]))


# ── status: why a coupon is not discounting ───────────────────────────────────
@pytest.mark.parametrize("coupon,expected", [
    ({"active": False}, C.DISABLED),
    ({"starts_at": NOW + 100}, C.SCHEDULED),
    ({"expires_at": NOW - 1}, C.EXPIRED),
    ({"max_redemptions": 5, "redeemed": 5}, C.EXHAUSTED),
    ({"max_redemptions": 5, "redeemed": 4}, C.ACTIVE),
    ({}, C.ACTIVE),
    (None, C.UNKNOWN),
])
def test_status_says_why(coupon, expected):
    """An operator looking at a coupon that is not working needs the reason, not a boolean."""
    assert C.status(coupon, NOW) == expected


def test_a_coupon_expiring_exactly_now_is_expired():
    # The boundary matters: a campaign ending at midnight must not discount at midnight.
    assert C.status({"expires_at": NOW}, NOW) == C.EXPIRED


def test_disabled_beats_every_other_reason():
    """One reason is shown, and 'somebody switched it off' is the one an operator acts on."""
    assert C.status({"active": False, "expires_at": NOW - 1}, NOW) == C.DISABLED


def test_remaining_is_none_when_uncapped():
    """A UI rendering "unlimited" and one rendering "999999" tell an operator different things."""
    assert C.remaining({"max_redemptions": 0}) is None
    assert C.remaining({"max_redemptions": 10, "redeemed": 3}) == 7
    assert C.remaining({"max_redemptions": 10, "redeemed": 99}) == 0


# ── the expiry that was never checked ─────────────────────────────────────────
def test_an_expired_coupon_does_not_discount():
    """`coupon_discount` looked at `active` and nothing else, so a campaign that ended in March
    kept discounting in July."""
    expired = {"type": "percent", "value": 50, "active": True, "expires_at": NOW - 1}
    assert pricing.coupon_discount(expired, 10000, now=NOW) == 0
    assert pricing.coupon_discount(expired, 10000) == 5000      # without a clock, unchanged


def test_a_scheduled_coupon_does_not_discount_early():
    future = {"type": "percent", "value": 50, "active": True, "starts_at": NOW + 100}
    assert pricing.coupon_discount(future, 10000, now=NOW) == 0


def test_a_live_coupon_still_discounts():
    live = {"type": "percent", "value": 50, "active": True,
            "starts_at": NOW - 10, "expires_at": NOW + 10}
    assert pricing.coupon_discount(live, 10000, now=NOW) == 5000


# ── the cap that was never enforced ───────────────────────────────────────────
def test_redeeming_counts():
    """Nothing incremented this. `max_redemptions` was a number an operator set and the service
    ignored entirely."""
    st = MemoryDataStore()
    b = _svc(st)
    run(b.upsert_coupon(code="SAVE", type="percent", value=10, max_redemptions=2))
    assert run(CouponStore(st).redeem("SAVE")) == (True, 1)
    assert run(CouponStore(st).redeem("SAVE")) == (True, 2)
    # atomic cap: the 3rd redeem is refused, counter unchanged
    assert run(CouponStore(st).redeem("SAVE", max_redemptions=2)) == (False, 2)


def test_a_capped_coupon_is_refused_once_it_is_used_up():
    st = MemoryDataStore()
    b = _svc(st)
    plan = _plan(b)
    run(b.upsert_coupon(code="ONCE", type="percent", value=10, max_redemptions=1))

    run(b.subscribe(tenant="t1", plan_id=plan.id, payment_token="pm", coupon_code="ONCE"))
    with pytest.raises(ValueError, match="COUPON_EXHAUSTED"):
        run(b.subscribe(tenant="t2", plan_id=plan.id, payment_token="pm", coupon_code="ONCE"))


# ── percent cap at creation (the audit gap: percent was never bounded ≤100) ───
def test_creating_a_percent_coupon_over_100_is_refused():
    st = MemoryDataStore()
    b = _svc(st)
    with pytest.raises(ValueError, match="COUPON_PERCENT_OUT_OF_RANGE"):
        run(b.upsert_coupon(code="TOOBIG", type="percent", value=150))
    run(b.upsert_coupon(code="FREE", type="percent", value=100))       # 100 = free, allowed
    # fixed/trial values are minor-units/days and stay uncapped
    run(b.upsert_coupon(code="BIGFIXED", type="fixed", value=999999))


# ── scoped (internal) creation forces public off ─────────────────────────────
def test_scoped_coupon_creation_forces_public_false():
    st = MemoryDataStore()
    b = _svc(st)
    c = run(b.create_scoped_coupon(code="GRANT", type="percent", value=20, public=True))
    assert c.public is False                    # the flag can never be set on this path
    assert run(b.get_coupon("GRANT"))["public"] is False


# ── attach a coupon to an EXISTING subscription (the post-signup gap) ─────────
def _sub(b, tenant="t1"):
    plan = _plan(b)
    return run(b.subscribe(tenant=tenant, plan_id=plan.id, payment_token="pm")), plan


def test_attach_coupon_applies_to_an_existing_subscription():
    st = MemoryDataStore()
    b = _svc(st)
    sub, _ = _sub(b)
    run(b.upsert_coupon(code="SAVE20", type="percent", value=20, max_redemptions=1))
    res = run(b.attach_coupon(subscription_id=sub.id, tenant="t1", coupon_code="SAVE20"))
    assert res["status"] == "attached"
    assert run(b._subs.fetch(sub.id)).coupon_code == "SAVE20"
    # the redemption was claimed (cap of 1 now consumed)
    assert C.remaining(run(b._coupons.get("SAVE20"))) == 0


def test_attach_is_idempotent_and_does_not_double_redeem():
    st = MemoryDataStore()
    b = _svc(st)
    sub, _ = _sub(b)
    run(b.upsert_coupon(code="ONCE", type="percent", value=20, max_redemptions=1))
    run(b.attach_coupon(subscription_id=sub.id, tenant="t1", coupon_code="ONCE"))
    again = run(b.attach_coupon(subscription_id=sub.id, tenant="t1", coupon_code="ONCE"))
    assert again["status"] == "unchanged"
    assert C.remaining(run(b._coupons.get("ONCE"))) == 0     # still 0 — NOT double-redeemed to -1/refused


def test_attach_enforces_the_redemption_cap_across_subscriptions():
    # A one-time grant attached to one sub cannot then be attached to another — the whole point.
    st = MemoryDataStore()
    b = _svc(st)
    s1, plan = _sub(b, "t1")
    s2 = run(b.subscribe(tenant="t2", plan_id=plan.id, payment_token="pm"))
    run(b.upsert_coupon(code="ONE", type="percent", value=20, max_redemptions=1))
    run(b.attach_coupon(subscription_id=s1.id, tenant="t1", coupon_code="ONE"))
    # Sequentially the derived status catches it first (EXHAUSTED); the atomic redeem CAS is the
    # backstop for the concurrent case. Either way the second attach is refused, fail-closed.
    with pytest.raises(ValueError, match="COUPON_EXHAUSTED"):
        run(b.attach_coupon(subscription_id=s2.id, tenant="t2", coupon_code="ONE"))


def test_attach_fails_closed_on_a_non_usable_coupon():
    st = MemoryDataStore()
    b = _svc(st)
    sub, _ = _sub(b)
    run(b.upsert_coupon(code="OLD", type="percent", value=20, expires_at=NOW - 1))
    with pytest.raises(ValueError, match="COUPON_EXPIRED"):
        run(b.attach_coupon(subscription_id=sub.id, tenant="t1", coupon_code="OLD"))
    with pytest.raises(ValueError, match="COUPON_UNKNOWN"):
        run(b.attach_coupon(subscription_id=sub.id, tenant="t1", coupon_code="NOSUCH"))
    assert run(b._subs.fetch(sub.id)).coupon_code == ""      # nothing attached on refusal


def test_attach_is_tenant_guarded_against_idor():
    st = MemoryDataStore()
    b = _svc(st)
    sub, _ = _sub(b, "t1")
    run(b.upsert_coupon(code="X", type="percent", value=20))
    # another tenant naming a real subscription id is told it does not exist, never mutated
    with pytest.raises(ValueError, match="SUBSCRIPTION_NOT_FOUND"):
        run(b.attach_coupon(subscription_id=sub.id, tenant="t2", coupon_code="X"))
    assert run(b._subs.fetch(sub.id)).coupon_code == ""


def test_attach_refuses_a_plan_scoped_coupon_for_a_different_plan():
    st = MemoryDataStore()
    b = _svc(st)
    sub, _ = _sub(b)
    run(b.upsert_coupon(code="OTHER", type="percent", value=20, plan_id="some_other_plan"))
    with pytest.raises(ValueError, match="COUPON_PLAN_MISMATCH"):
        run(b.attach_coupon(subscription_id=sub.id, tenant="t1", coupon_code="OTHER"))
    # a mismatch never consumes a redemption
    assert C.remaining(run(b._coupons.get("OTHER"))) is None


def test_an_uncapped_coupon_never_runs_out():
    st = MemoryDataStore()
    b = _svc(st)
    plan = _plan(b)
    run(b.upsert_coupon(code="OPEN", type="percent", value=10, max_redemptions=0))
    for tenant in ("t1", "t2", "t3"):
        run(b.subscribe(tenant=tenant, plan_id=plan.id, payment_token="pm", coupon_code="OPEN"))


def test_an_expired_coupon_is_refused_at_signup():
    st = MemoryDataStore()
    b = _svc(st)
    plan = _plan(b)
    run(b.upsert_coupon(code="OLD", type="percent", value=10, expires_at=NOW - 1))
    with pytest.raises(ValueError, match="COUPON_EXPIRED"):
        run(b.subscribe(tenant="t1", plan_id=plan.id, payment_token="pm", coupon_code="OLD"))


def test_a_disabled_coupon_is_refused_at_signup():
    st = MemoryDataStore()
    b = _svc(st)
    plan = _plan(b)
    run(b.upsert_coupon(code="OFF", type="percent", value=10, active=False))
    with pytest.raises(ValueError, match="COUPON_DISABLED"):
        run(b.subscribe(tenant="t1", plan_id=plan.id, payment_token="pm", coupon_code="OFF"))


def test_an_unknown_code_is_refused_rather_than_silently_ignored():
    """Accepting a typo'd code and charging full price is a support ticket; refusing it is an
    error message the customer can read. And it says UNKNOWN, not EXPIRED — "there is no such
    code" and "that campaign ended" are different things to tell somebody."""
    st = MemoryDataStore()
    b = _svc(st)
    plan = _plan(b)
    with pytest.raises(ValueError, match="COUPON_UNKNOWN"):
        run(b.subscribe(tenant="t1", plan_id=plan.id, payment_token="pm", coupon_code="NOPE"))


def test_the_cap_counts_customers_not_billing_cycles():
    """max_redemptions means "how many customers may use this". Counting per cycle would make a
    monthly coupon consume its own cap every month and retire itself for a single customer."""
    st = MemoryDataStore()
    b = _svc(st)
    plan = _plan(b)
    run(b.upsert_coupon(code="MONTHLY", type="percent", value=10, max_redemptions=1))
    sub = run(b.subscribe(tenant="t1", plan_id=plan.id, payment_token="pm",
                          coupon_code="MONTHLY"))
    run(b.run_cycle(sub.id))
    run(b.run_cycle(sub.id))
    assert run(b.get_coupon("MONTHLY"))["redeemed"] == 1


# ── editing must not resurrect a spent coupon ─────────────────────────────────
def test_editing_a_coupon_does_not_reset_its_redemptions():
    """A full-object write was the only way to edit, and it zeroed the counter — so fixing a typo
    silently resurrected a coupon customers had already used up."""
    st = MemoryDataStore()
    b = _svc(st)
    run(b.upsert_coupon(code="SAVE", type="percent", value=10, max_redemptions=5))
    run(CouponStore(st).redeem("SAVE"))
    run(CouponStore(st).redeem("SAVE"))

    run(b.update_coupon("SAVE", value=15))
    after = run(b.get_coupon("SAVE"))
    assert after["redeemed"] == 2, "the redemption counter was reset by an edit"
    assert after["value"] == 15


def test_redeemed_cannot_be_written_through_an_edit():
    st = MemoryDataStore()
    b = _svc(st)
    run(b.upsert_coupon(code="SAVE", type="percent", value=10))
    run(CouponStore(st).redeem("SAVE"))
    with pytest.raises(ValueError, match="NOT_EDITABLE"):
        run(b.update_coupon("SAVE", redeemed=0))


def test_the_store_itself_refuses_to_write_the_counter():
    """The service filters `redeemed` out before it ever reaches the store, so this guard is
    defence in depth — and an untested guard is a rule that can rot. `patch` is a public method,
    and the next caller of it will not know the counter is off limits unless the store says so.
    """
    st = MemoryDataStore()
    b = _svc(st)
    run(b.upsert_coupon(code="SAVE", type="percent", value=10))
    run(CouponStore(st).redeem("SAVE"))

    run(CouponStore(st).patch("SAVE", {"redeemed": 0, "value": 25}))
    row = run(CouponStore(st).get("SAVE"))
    assert row["redeemed"] == 1, "the store let a caller rewrite the redemption counter"
    assert row["value"] == 25, "the rest of the edit did not land"


def test_the_store_refuses_to_rename_a_coupon_through_patch():
    # The code is the key. Changing it through a patch would write a row whose stored code no
    # longer matches where it lives.
    st = MemoryDataStore()
    b = _svc(st)
    run(b.upsert_coupon(code="SAVE", type="percent", value=10))
    run(CouponStore(st).patch("SAVE", {"code": "OTHER"}))
    assert run(CouponStore(st).get("SAVE"))["code"] == "SAVE"


def test_a_partial_edit_leaves_the_other_fields_alone():
    st = MemoryDataStore()
    b = _svc(st)
    run(b.upsert_coupon(code="SAVE", type="percent", value=10, max_redemptions=5,
                        expires_at=NOW + 500, note="spring"))
    run(b.update_coupon("SAVE", active=False))
    after = run(b.get_coupon("SAVE"))
    assert after["value"] == 10 and after["max_redemptions"] == 5
    assert after["expires_at"] == NOW + 500 and after["note"] == "spring"
    assert after["active"] is False


def test_editing_an_unknown_coupon_is_refused():
    with pytest.raises(ValueError, match="COUPON_NOT_FOUND"):
        run(_svc(MemoryDataStore()).update_coupon("GHOST", value=1))


# ── listing: the gap that started this ────────────────────────────────────────
def test_coupons_can_be_listed_at_all():
    """There was no route and no store method: an operator had to remember every code they had
    ever created."""
    st = MemoryDataStore()
    b = _svc(st)
    run(b.upsert_coupon(code="A", type="percent", value=10))
    run(b.upsert_coupon(code="B", type="fixed", value=500))
    codes = {c["code"] for c in run(b.list_coupons())}
    assert codes == {"A", "B"}


def test_a_listing_says_why_each_coupon_is_or_is_not_working():
    st = MemoryDataStore()
    b = _svc(st)
    run(b.upsert_coupon(code="LIVE", type="percent", value=10))
    run(b.upsert_coupon(code="DEAD", type="percent", value=10, expires_at=NOW - 1))
    got = {c["code"]: c["status"] for c in run(b.list_coupons())}
    assert got == {"LIVE": C.ACTIVE, "DEAD": C.EXPIRED}


def test_a_listing_can_be_filtered_by_status():
    st = MemoryDataStore()
    b = _svc(st)
    run(b.upsert_coupon(code="LIVE", type="percent", value=10))
    run(b.upsert_coupon(code="DEAD", type="percent", value=10, expires_at=NOW - 1))
    assert [c["code"] for c in run(b.list_coupons(status=C.EXPIRED))] == ["DEAD"]


def test_an_unknown_status_filter_is_refused():
    with pytest.raises(ValueError, match="UNKNOWN_STATUS"):
        run(_svc(MemoryDataStore()).list_coupons(status="banana"))


# ── delete ────────────────────────────────────────────────────────────────────
def test_a_coupon_can_be_deleted():
    st = MemoryDataStore()
    b = _svc(st)
    run(b.upsert_coupon(code="OOPS", type="percent", value=10))
    assert run(b.delete_coupon("OOPS")) is True
    assert run(b.get_coupon("OOPS")) is None


def test_deleting_a_coupon_that_is_not_there_reports_it():
    assert run(_svc(MemoryDataStore()).delete_coupon("GHOST")) is False
