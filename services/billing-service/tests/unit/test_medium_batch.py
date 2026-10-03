"""Non-security MEDIUM remediation (billing-service):
  004  change_plan refuses an interval the target plan does not offer (PLAN_INTERVAL_UNAVAILABLE).
  005  a fixed-value coupon in a different currency than the charge is refused (COUPON_CURRENCY_MISMATCH).
  007  wallet_topup honours an explicit idempotency_key so deliberate identical repeats both charge.
"""
from __future__ import annotations

import pytest

from app.adapters.datastore.memory import MemoryDataStore
from app.core.models import now_ts

from tests.unit.test_audit_fixes import _svc, run


def _plan(b, prices, name="P"):
    return run(b.create_plan(name=name, prices=prices, limits={}))


# ── 004: change_plan interval availability ────────────────────────────────────
def test_change_plan_refuses_an_interval_the_target_plan_does_not_offer():
    st = MemoryDataStore(); b = _svc(st)
    src = _plan(b, [{"interval": "month", "amount_minor": 1000, "currency": "SAR"}], "src")
    # target offers ONLY month — a change requesting "year" must be refused, not fall back to prices[0].
    dst = _plan(b, [{"interval": "month", "amount_minor": 2000, "currency": "SAR"}], "dst")
    sub = run(b.subscribe(tenant="t1", plan_id=src.id, payment_token="pm", interval="month"))
    with pytest.raises(ValueError, match="PLAN_INTERVAL_UNAVAILABLE"):
        run(b.change_plan(sub.id, new_plan_id=dst.id, interval="year"))


def test_change_plan_allows_an_offered_interval():
    st = MemoryDataStore(); b = _svc(st)
    src = _plan(b, [{"interval": "month", "amount_minor": 1000, "currency": "SAR"}], "src")
    dst = _plan(b, [{"interval": "month", "amount_minor": 500, "currency": "SAR"}], "dst")
    sub = run(b.subscribe(tenant="t1", plan_id=src.id, payment_token="pm", interval="month"))
    res = run(b.change_plan(sub.id, new_plan_id=dst.id, interval="month"))
    assert res["subscription_id"] == sub.id


# ── 005: fixed-coupon currency must match the charge ──────────────────────────
def test_subscribe_refuses_a_fixed_coupon_in_a_foreign_currency():
    st = MemoryDataStore(); b = _svc(st)
    plan = _plan(b, [{"interval": "month", "amount_minor": 10000, "currency": "SAR"}])
    run(b.upsert_coupon(code="USD50", type="fixed", value=5000, currency="USD"))
    with pytest.raises(ValueError, match="COUPON_CURRENCY_MISMATCH"):
        run(b.subscribe(tenant="t1", plan_id=plan.id, payment_token="pm", coupon_code="USD50"))


def test_subscribe_accepts_a_fixed_coupon_in_the_charge_currency():
    st = MemoryDataStore(); b = _svc(st)
    plan = _plan(b, [{"interval": "month", "amount_minor": 10000, "currency": "SAR"}])
    run(b.upsert_coupon(code="SAR50", type="fixed", value=5000, currency="SAR"))
    sub = run(b.subscribe(tenant="t1", plan_id=plan.id, payment_token="pm", coupon_code="SAR50"))
    assert sub.coupon_code == "SAR50"


def test_attach_coupon_refuses_a_foreign_currency_fixed_coupon():
    st = MemoryDataStore(); b = _svc(st)
    plan = _plan(b, [{"interval": "month", "amount_minor": 10000, "currency": "SAR"}])
    sub = run(b.subscribe(tenant="t1", plan_id=plan.id, payment_token="pm"))
    run(b.upsert_coupon(code="USD50", type="fixed", value=5000, currency="USD"))
    with pytest.raises(ValueError, match="COUPON_CURRENCY_MISMATCH"):
        run(b.attach_coupon(subscription_id=sub.id, tenant="t1", coupon_code="USD50"))


def test_percent_coupon_currency_is_irrelevant():
    # A percent coupon has no denomination — its currency field must never block attachment.
    st = MemoryDataStore(); b = _svc(st)
    plan = _plan(b, [{"interval": "month", "amount_minor": 10000, "currency": "SAR"}])
    run(b.upsert_coupon(code="P10", type="percent", value=10, currency="USD"))
    sub = run(b.subscribe(tenant="t1", plan_id=plan.id, payment_token="pm", coupon_code="P10"))
    assert sub.coupon_code == "P10"


# ── 007: explicit idempotency_key distinguishes deliberate repeats ────────────
def test_explicit_idempotency_key_allows_a_deliberate_repeat_topup():
    st = MemoryDataStore(); b = _svc(st)
    a = run(b.wallet_topup(tenant="t1", amount_minor=5000, payment_token="pm", fund="credits",
                           idempotency_key="k1"))
    c = run(b.wallet_topup(tenant="t1", amount_minor=5000, payment_token="pm", fund="credits",
                           idempotency_key="k2"))
    assert not c.get("duplicate")
    assert c["invoice_id"] != a["invoice_id"]
    invs, _ = run(b._invoices.browse("t1", limit=10))
    assert len(invs) == 2


def test_same_idempotency_key_still_dedupes():
    st = MemoryDataStore(); b = _svc(st)
    a = run(b.wallet_topup(tenant="t1", amount_minor=5000, payment_token="pm", fund="credits",
                           idempotency_key="k1"))
    c = run(b.wallet_topup(tenant="t1", amount_minor=5000, payment_token="pm", fund="credits",
                           idempotency_key="k1"))
    assert c.get("duplicate") is True and c["invoice_id"] == a["invoice_id"]
