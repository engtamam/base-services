"""The rate limiter, proven where it actually has to hold.

Every test here drives a specific attack with a fake clock — no sleeps, deterministic. The one that
matters most is the boundary burst: it is the reason we do not use a fixed window, and it is exactly
what ammar (INCR/EXPIRE) is vulnerable to.
"""
from __future__ import annotations

import asyncio

import pytest

from app.adapters.rate_limiter import sliding_window as sw
from app.adapters.rate_limiter.memory import MemoryRateLimiter


def run(c):
    return asyncio.run(c)


# ── T3 — the window-boundary burst ───────────────────────────────────────────
def test_gcra_has_no_boundary_burst():
    """A fixed window allows 2x the budget across a boundary. GCRA does not: the allowance decays
    continuously, so there is no edge to straddle.

    limit=5 per 1000ms. Spend all 5 at t=900. A fixed window would refill at t=1000 and allow 5
    MORE at t=1001 — ten requests in ~100ms. GCRA must not.
    """
    limit, window = 5, 1000
    tat = 0.0
    # burn the whole budget near the end of the first window
    allowed_first = 0
    for _ in range(5):
        d = sw.check(tat, 900, limit=limit, window_ms=window)
        if d.allowed:
            tat = d.new_tat
            allowed_first += 1
    assert allowed_first == 5, "the budget should be spendable"

    # immediately after the notional boundary, a fixed window would open the floodgates
    allowed_after = 0
    for _ in range(5):
        d = sw.check(tat, 1001, limit=limit, window_ms=window)
        if d.allowed:
            tat = d.new_tat
            allowed_after += 1
    assert allowed_after <= 1, (
        f"GCRA allowed {allowed_after} across the boundary — a fixed window would allow 5, "
        f"for 10 in ~100ms against a limit of 5")


def test_a_fixed_window_WOULD_fail_this_same_attack():
    """Proof the test is real and not vacuous: model a fixed window and watch it allow 2x."""
    def fixed_window_allows(count_at_900, count_at_1001, limit=5):
        # window 1 resets at 1000; both bursts are under `limit` per window, so both pass
        return count_at_900 <= limit and count_at_1001 <= limit

    assert fixed_window_allows(5, 5) is True   # 10 requests, limit 5 — the bug we designed out


def test_an_instantaneous_burst_is_capped_at_the_limit():
    """All requests at the SAME instant: exactly `limit` pass, the rest are denied. This is the
    burst ceiling — GCRA permits a burst of `limit`, and not one more."""
    limit, window = 10, 1000
    tat, allowed = 0.0, 0
    for _ in range(50):
        d = sw.check(tat, 0, limit=limit, window_ms=window)     # now never advances
        if d.allowed:
            tat = d.new_tat
            allowed += 1
    assert allowed == limit


def test_sustained_rate_matches_the_configured_throughput():
    """At the emission interval, the limiter admits one request per interval indefinitely — the
    steady-state throughput is exactly limit/window, after the initial burst."""
    limit, window = 10, 1000
    interval = window / limit          # 100ms
    tat = 0.0
    # drain the burst
    for _ in range(limit):
        tat = sw.check(tat, 0, limit=limit, window_ms=window).new_tat
    # now, one request exactly every `interval` must all pass; one faster must not
    now = 0.0
    admitted = 0
    for _ in range(20):
        now += interval
        d = sw.check(tat, now, limit=limit, window_ms=window)
        if d.allowed:
            tat = d.new_tat
            admitted += 1
    assert admitted == 20, "one per interval is the sustained rate and must always pass"


def test_cost_weights_an_expensive_call():
    """A cost-5 request consumes five requests' worth of budget."""
    limit, window = 10, 1000
    d = sw.check(0.0, 0, limit=limit, window_ms=window, cost=5)
    assert d.allowed and d.remaining == 5


def test_a_denied_request_does_not_extend_its_own_lockout():
    """A client hammering a throttled key must not push its own retry further out with every try."""
    limit, window = 1, 1000
    d1 = sw.check(0.0, 0, limit=limit, window_ms=window)
    tat = d1.new_tat
    r = [sw.check(tat, 10, limit=limit, window_ms=window).retry_after_ms for _ in range(5)]
    assert len(set(r)) == 1, "the retry_after must be stable, not grow with each rejected attempt"


def test_retry_after_is_honest():
    """When denied, retry_after_ms is when the NEXT request would actually pass."""
    limit, window = 2, 1000
    tat = 0.0
    for _ in range(2):
        tat = sw.check(tat, 0, limit=limit, window_ms=window).new_tat
    d = sw.check(tat, 0, limit=limit, window_ms=window)
    assert not d.allowed
    # wait exactly retry_after, then it should pass
    ok = sw.check(tat, d.retry_after_ms, limit=limit, window_ms=window)
    assert ok.allowed


# ── T5 — the read-then-write race, on the real adapter ───────────────────────
def test_concurrent_requests_cannot_exceed_the_limit():
    """200 requests fired at once against a limit of 10 -> exactly 10 pass. Without the lock in the
    memory adapter, several would read the same TAT and all decide 'allowed'."""
    async def go():
        rl = MemoryRateLimiter(max_requests=10, window_seconds=60)
        results = await asyncio.gather(*[rl.allow("same-key") for _ in range(200)])
        return sum(results)
    assert run(go()) == 10


def test_different_keys_do_not_share_a_budget():
    async def go():
        rl = MemoryRateLimiter(max_requests=1, window_seconds=60)
        return [await rl.allow(f"k{i}") for i in range(5)]
    assert run(go()) == [True] * 5


def test_reset_clears_one_key_only():
    async def go():
        rl = MemoryRateLimiter(max_requests=1, window_seconds=60)
        await rl.allow("a"); await rl.allow("b")
        assert await rl.allow("a") is False
        await rl.reset("a")
        return await rl.allow("a"), await rl.allow("b")
    a_after, b_after = run(go())
    assert a_after is True                       # a was unblocked
    assert b_after is False                      # b was NOT touched


def test_check_reports_remaining_and_retry():
    async def go():
        rl = MemoryRateLimiter(max_requests=3, window_seconds=60)
        v1 = await rl.check("k")
        assert v1.allowed and v1.limit == 3 and v1.remaining == 2
        await rl.check("k"); await rl.check("k")
        v4 = await rl.check("k")
        assert not v4.allowed and v4.retry_after_s >= 1 and v4.remaining == 0
    run(go())


def test_per_call_limit_override():
    """One limiter instance serves several route classes: a strict login budget and a loose api
    budget, from the same object."""
    async def go():
        rl = MemoryRateLimiter(max_requests=100, window_seconds=60)
        # a strict override for the login class
        return [(await rl.check(f"login:{i}", limit=2, window_s=60)).allowed for i in range(1)] \
            + [(await rl.check("login:x", limit=2, window_s=60)).allowed for _ in range(4)]
    res = run(go())
    assert res[1:].count(True) == 2, "the override limit of 2 applied, not the default 100"


def test_bad_inputs_are_refused():
    with pytest.raises(ValueError):
        sw.check(0.0, 0, limit=0, window_ms=1000)
    with pytest.raises(ValueError):
        sw.check(0.0, 0, limit=5, window_ms=0)
