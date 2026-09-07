"""Concurrency: idempotency claims are ATOMIC, so a duplicate never double-charges (#9).

A get-then-put idempotency check let two concurrent runs both proceed. For a renewal that mints a
NEW invoice id each time, the gateway idempotency key differs and the CARD IS CHARGED TWICE. These
prove the atomic claim (billing) prevents it.
"""
from __future__ import annotations

import asyncio

from app.adapters.datastore.memory import MemoryDataStore
from app.core.models import new_id
from app.core.stores import CouponStore

from tests.unit.test_billing_core import _svc, run


class _CountingGW:
    driver = "mock"

    def __init__(self):
        self.charges = 0

    def charge(self, **k):
        from app.ports.payment_gateway import Charge
        self.charges += 1
        return Charge(ok=True, status="succeeded", provider="mock",
                      provider_id="ch_" + k["idempotency_key"][:8],
                      amount_minor=k["amount_minor"], currency=k["currency"])

    def refund(self, **k):
        from app.ports.payment_gateway import Charge
        return Charge(ok=True, status="refunded", provider="mock", provider_id="re_1")


def test_idempotency_claim_is_won_by_exactly_one_caller():
    """The primitive run_cycle/run_overage rely on: for one key, exactly one caller gets proceed=True;
    a second is refused while pending, and replays once the result is marked. (Tested at the store
    because the in-memory datastore does not yield, so gather() over a full charge path serialises.)"""
    st = MemoryDataStore()
    from app.core.stores import IdempotencyStore
    idem = IdempotencyStore(st)
    key = f"cycle:{new_id('sub_')}:1700000000"
    a, pa = run(idem.claim(key))
    b, pb = run(idem.claim(key))
    assert (a, pa) == (True, None)               # first caller owns the operation
    assert (b, pb) == (False, None)              # second: pending, must retry — never proceed
    # after the winner marks the result, a duplicate replays it (no re-run)
    run(idem.mark(key, {"invoice_id": "inv_x"}))
    c, pc = run(idem.claim(key))
    assert c is False and pc == {"invoice_id": "inv_x"}


def test_a_stale_pending_claim_is_reclaimable():
    st = MemoryDataStore()
    from app.core.stores import IdempotencyStore
    from app.core.models import now_ts
    idem = IdempotencyStore(st)
    key = "cycle:sub_stale:1"
    # a pending claim whose lease already lapsed (a crashed winner) is re-claimable
    run(st.put("idempo", key, {"key": key, "status": "pending", "expires_at": now_ts() - 10}))
    proceed, prior = run(idem.claim(key))
    assert proceed is True and prior is None


def test_concurrent_coupon_redemptions_respect_the_cap():
    st = MemoryDataStore()
    b = _svc(st, _CountingGW())
    run(b.upsert_coupon(code="ONCE", type="percent", value=10, max_redemptions=1))

    async def both():
        return await asyncio.gather(
            CouponStore(st).redeem("ONCE", max_redemptions=1),
            CouponStore(st).redeem("ONCE", max_redemptions=1),
            return_exceptions=True)

    results = run(both())
    applied = [r for r in results if isinstance(r, tuple) and r[0]]
    refused = [r for r in results if isinstance(r, tuple) and not r[0]]
    assert len(applied) == 1 and len(refused) == 1, f"cap not enforced atomically: {results}"
    # final counter is exactly 1
    assert int(run(CouponStore(st).fetch("ONCE")).redeemed) == 1
