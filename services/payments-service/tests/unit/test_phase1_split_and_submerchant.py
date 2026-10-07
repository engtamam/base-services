"""Phase 1 — server-authoritative split planning + the sub-merchant mapping, tested adversarially.

The split is where money is apportioned, so every hostile shape (negative/bool/non-int shares, dupes,
over-total, empty) must be REFUSED and nothing computed; the sub-merchant mapping gates payouts on an
ACTIVE status and isolates tenants (no cross-tenant destination, no "*"-hijack).
"""
from __future__ import annotations

import asyncio

import pytest

from app.adapters.datastore.memory import MemoryDataStore
from app.core.split import SplitError, plan_split
from app.core.submerchant import (ST_ACTIVE, ST_PENDING, ST_REJECTED, SubMerchantError, SubMerchantStore)
from app.ports.payment_gateway import SplitDestination


def run(c):
    return asyncio.run(c)


# ═══════════════════════════════════ plan_split ═════════════════════════════════
def test_single_destination_platform_fee_is_remainder():
    p = plan_split(total_minor=10000, currency="sar", destinations=[{"destination_id": "d1", "amount_minor": 9000}])
    assert p.platform_fee_minor == 1000
    assert p.currency == "SAR"                       # upper-cased
    assert [d.to_dict() for d in p.destinations] == [{"destination_id": "d1", "amount_minor": 9000}]


def test_multiple_destinations_sum_under_total():
    p = plan_split(total_minor=10000, currency="SAR",
                   destinations=[{"destination_id": "a", "amount_minor": 4000},
                                 {"destination_id": "b", "amount_minor": 3000}])
    assert p.platform_fee_minor == 3000


def test_shares_equal_total_means_zero_fee():
    p = plan_split(total_minor=5000, currency="SAR", destinations=[{"destination_id": "d", "amount_minor": 5000}])
    assert p.platform_fee_minor == 0


def test_shares_over_total_is_refused():
    with pytest.raises(SplitError):
        plan_split(total_minor=5000, currency="SAR", destinations=[{"destination_id": "d", "amount_minor": 5001}])


def test_duplicate_destination_is_refused():
    with pytest.raises(SplitError):
        plan_split(total_minor=10000, currency="SAR",
                   destinations=[{"destination_id": "d", "amount_minor": 1000},
                                 {"destination_id": "d", "amount_minor": 1000}])


@pytest.mark.parametrize("bad_amount", [0, -1, -100])
def test_non_positive_share_is_refused(bad_amount):
    with pytest.raises(SplitError):
        plan_split(total_minor=10000, currency="SAR",
                   destinations=[{"destination_id": "d", "amount_minor": bad_amount}])


def test_bool_share_is_refused_not_coerced_to_one():
    # True is an int subclass — it must NOT sneak through as amount 1.
    with pytest.raises(SplitError):
        plan_split(total_minor=10000, currency="SAR", destinations=[{"destination_id": "d", "amount_minor": True}])


@pytest.mark.parametrize("bad", ["100", 10.0, None])
def test_non_integer_share_is_refused(bad):
    with pytest.raises(SplitError):
        plan_split(total_minor=10000, currency="SAR", destinations=[{"destination_id": "d", "amount_minor": bad}])


def test_empty_destinations_is_refused():
    with pytest.raises(SplitError):
        plan_split(total_minor=10000, currency="SAR", destinations=[])


def test_empty_destination_id_is_refused():
    with pytest.raises(SplitError):
        plan_split(total_minor=10000, currency="SAR", destinations=[{"destination_id": "  ", "amount_minor": 100}])


@pytest.mark.parametrize("bad_total", [0, -1, True, "100", 10.5])
def test_bad_total_is_refused(bad_total):
    with pytest.raises(SplitError):
        plan_split(total_minor=bad_total, currency="SAR", destinations=[{"destination_id": "d", "amount_minor": 1}])


def test_missing_currency_is_refused():
    with pytest.raises(SplitError):
        plan_split(total_minor=1000, currency="", destinations=[{"destination_id": "d", "amount_minor": 1}])


def test_accepts_splitdestination_objects_too():
    p = plan_split(total_minor=1000, currency="SAR", destinations=[SplitDestination("d", 600)])
    assert p.platform_fee_minor == 400


def test_garbage_destination_entry_is_refused():
    with pytest.raises(SplitError):
        plan_split(total_minor=1000, currency="SAR", destinations=["not-a-dict"])


# ═══════════════════════════════ SubMerchantStore ═══════════════════════════════
def _store():
    return SubMerchantStore(MemoryDataStore())


def test_active_mapping_returns_destination():
    s = _store()
    run(s.set(tenant="acme", provider="tap", destination_id="dst_1"))
    assert run(s.destination_id("acme")) == "dst_1"
    assert run(s.status("acme")) == ST_ACTIVE


def test_pending_mapping_is_not_payable():
    s = _store()
    run(s.set(tenant="acme", provider="tap", destination_id="dst_1", status=ST_PENDING))
    assert run(s.destination_id("acme")) == ""        # fail-closed: pending cannot receive a split
    assert run(s.status("acme")) == ST_PENDING


def test_rejected_mapping_is_not_payable():
    s = _store()
    run(s.set(tenant="acme", provider="tap", destination_id="dst_1", status=ST_REJECTED))
    assert run(s.destination_id("acme")) == ""


def test_set_re_upsert_advances_pending_to_active():
    s = _store()
    run(s.set(tenant="acme", provider="tap", destination_id="dst_1", status=ST_PENDING))
    assert run(s.destination_id("acme")) == ""          # pending → not payable
    run(s.set(tenant="acme", provider="tap", destination_id="dst_1", status=ST_ACTIVE))
    assert run(s.destination_id("acme")) == "dst_1"     # re-upsert to active → payable


def test_unknown_status_is_refused():
    s = _store()
    with pytest.raises(SubMerchantError):
        run(s.set(tenant="acme", provider="tap", destination_id="d", status="whatever"))


def test_empty_tenant_or_destination_is_refused():
    s = _store()
    with pytest.raises(SubMerchantError):
        run(s.set(tenant="", provider="tap", destination_id="d"))
    with pytest.raises(SubMerchantError):
        run(s.set(tenant="acme", provider="tap", destination_id="  "))


def test_mapping_is_tenant_isolated():
    s = _store()
    run(s.set(tenant="a", provider="tap", destination_id="dst_a"))
    assert run(s.destination_id("a")) == "dst_a"
    assert run(s.destination_id("b")) == ""           # b has none — never sees a's destination


def test_star_tenant_cannot_collide_with_another():
    s = _store()
    run(s.set(tenant="*", provider="tap", destination_id="dst_star"))
    assert run(s.destination_id("victim")) == ""
    assert s._key("*") != s._key("victim") != s._key("")


def test_clear_removes_the_mapping():
    s = _store()
    run(s.set(tenant="acme", provider="tap", destination_id="dst_1"))
    assert run(s.clear("acme")) is True
    assert run(s.status("acme")) == "none"
