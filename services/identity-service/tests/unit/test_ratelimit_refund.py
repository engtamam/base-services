"""Count only genuine failures (T6, ammar's insight, generalised to config).

A correct password on an account pending approval returns 403 and must NOT spend the brute-force
budget — or the user is locked out WHILE they wait for the approval that would let them in, and the
approval does not clear it. Only real credential failures (401) should count.
"""
from __future__ import annotations

import asyncio

from app.adapters.rate_limiter.memory import MemoryRateLimiter


def run(c):
    return asyncio.run(c)


def test_a_refund_restores_the_budget():
    async def go():
        rl = MemoryRateLimiter(max_requests=3, window_seconds=60)
        k = "login:sara"
        await rl.check(k)                 # spend 1 (remaining 2)
        v = await rl.check(k)             # spend 1 (remaining 1)
        assert v.remaining == 1
        await rl.refund(k)                # this attempt did not count (a 403) -> give it back
        v2 = await rl.check(k)
        assert v2.remaining == 1, "the refund returned one, so this spend lands us back at 1"
    run(go())


def test_refund_never_banks_future_credit():
    """Refunding more than was spent must not create a budget larger than the limit."""
    async def go():
        rl = MemoryRateLimiter(max_requests=2, window_seconds=60)
        k = "k"
        await rl.check(k)
        for _ in range(5):
            await rl.refund(k)            # over-refund
        # still only `limit` requests available, never more
        allowed = 0
        for _ in range(10):
            if (await rl.check(k)).allowed:
                allowed += 1
        assert allowed == 2
    run(go())


def test_refund_on_an_unknown_key_is_a_noop():
    async def go():
        rl = MemoryRateLimiter(max_requests=2, window_seconds=60)
        await rl.refund("never-seen")     # must not raise, must not create the key
        assert (await rl.check("never-seen")).remaining == 1
    run(go())


def test_counted_surface_only_spends_on_counted_status():
    """The end-to-end shape: 20 requests, but only the ones that 'count' spend budget. Modelled
    here with explicit refunds for the non-counting ones (the middleware does this from the real
    status)."""
    async def go():
        rl = MemoryRateLimiter(max_requests=5, window_seconds=60)
        k = "login:x"
        # 10 correct-password-but-pending (403) attempts: each checks then refunds -> net zero
        for _ in range(10):
            await rl.check(k)
            await rl.refund(k)            # 403 is not in count-statuses
        # a genuine wrong password (401) now still has the full budget
        allowed = 0
        for _ in range(6):
            if (await rl.check(k)).allowed:
                allowed += 1
        assert allowed == 5, "the 403s did not erode the budget; the 401s are limited normally"
    run(go())
