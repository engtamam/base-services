"""Regression: a coupon CREATED by the store must be redeemable — the put/cas divergence.

The latent production bug (identity's refresh-rotation class): a row created with a plain `put()`
carries no `version`, so a later version-pinned `cas(coll, key, version, …)` never matches it on the
postgres adapter (its version pin is `WHERE (value->>'version')::bigint = expected`, and the
expected=0 create branch is `INSERT … ON CONFLICT DO NOTHING`). The update no-ops for ever in
production while the LAX in-memory cas (versionless row → 0 → matched) kept every test green.

For coupons that meant `CouponStore.upsert()` created a versionless row and `redeem()` (a
version-pinned cas) could never count a use — every redemption 400'd on postgres. These tests drive
create → version-pinned update and assert it SUCCEEDS, and pin the corrected memory-cas semantics so
the adapter can no longer hide the class.
"""
from __future__ import annotations

import asyncio

from app.adapters.datastore.memory import MemoryDataStore
from app.core.models import Coupon, iso
from app.core.stores import CouponStore


def run(c):
    return asyncio.run(c)


def test_created_coupon_is_redeemable_via_version_pinned_cas() -> None:
    """upsert() (create) → redeem() (version-pinned cas) must succeed.

    Fails against the old code: `upsert` used put() → the row had no version, and once the memory
    adapter matches postgres, redeem's cas(code, 0, …) is insert-if-absent against a present row and
    returns False, so the coupon is never redeemable.
    """
    store = MemoryDataStore()
    coupons = CouponStore(store)

    run(coupons.upsert(Coupon(code="SAVE", type="percent", value=10,
                              max_redemptions=2, created=iso())))

    # The freshly created row carries a version (so a version-pinned update can match it).
    row = run(store.get("coupons", "SAVE"))
    assert row is not None and int(row.get("version", 0)) >= 1

    applied, total = run(coupons.redeem("SAVE", max_redemptions=2))
    assert applied and total == 1
    applied, total = run(coupons.redeem("SAVE", max_redemptions=2))
    assert applied and total == 2
    # Cap enforced by the same cas loop.
    applied, total = run(coupons.redeem("SAVE", max_redemptions=2))
    assert not applied and total == 2


def test_memory_cas_expected_zero_is_strict_insert_if_absent() -> None:
    """expected_version=0 must REFUSE any existing row — versionless (put-created) ones included.

    This is the semantic that makes the memory adapter match postgres' INSERT … ON CONFLICT DO
    NOTHING. The old lax cas returned True here (versionless implicit 0 == expected 0), which is the
    exact behaviour that hid the create-via-put bug.
    """
    store = MemoryDataStore()
    run(store.put("coll", "k", {"id": "k"}))            # a versionless, put-created row
    assert run(store.cas("coll", "k", 0, {"id": "k"})) is False

    # A genuinely absent key is claimed and stamped version=1.
    assert run(store.cas("coll", "fresh", 0, {"id": "fresh"})) is True
    assert int(run(store.get("coll", "fresh"))["version"]) == 1
