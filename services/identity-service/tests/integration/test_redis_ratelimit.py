"""The redis rate limiter, against a REAL Redis.

Skips without RATELIMIT_TEST_URL — the same discipline as the postgres and MinIO contracts: a
limiter proven only against a fake has not been proven. The atomic Lua path and the outage-degrade
path cannot be exercised in memory.

    docker run -d -p 6379:6379 redis:7-alpine
    RATELIMIT_TEST_URL=redis://127.0.0.1:6379/0 pytest tests/integration/test_redis_ratelimit.py
"""
from __future__ import annotations

import asyncio
import os
import uuid

import pytest

_URL = os.environ.get("RATELIMIT_TEST_URL", "")
pytestmark = pytest.mark.skipif(not _URL, reason="RATELIMIT_TEST_URL not set — no real Redis")


def run(c):
    return asyncio.run(c)


def _limiter(max_requests=10, window=60, degrade="local"):
    from app.adapters.rate_limiter.redis import RedisRateLimiter
    return RedisRateLimiter(_URL, max_requests, window, degrade=degrade)


def _key():
    return f"test:{uuid.uuid4().hex}"


def test_it_limits_against_real_redis():
    async def go():
        rl = _limiter(max_requests=5, window=60)
        k = _key()
        results = [(await rl.check(k)).allowed for _ in range(8)]
        await rl.close()
        return results
    res = run(go())
    assert res.count(True) == 5 and res[5:] == [False, False, False]


def test_the_lua_path_is_atomic_under_concurrency():
    """200 concurrent requests at a limit of 10 -> exactly 10 pass. A GET-then-SET limiter would let
    many more through; the single Lua script cannot be interleaved."""
    async def go():
        rl = _limiter(max_requests=10, window=60)
        k = _key()
        results = await asyncio.gather(*[rl.check(k) for _ in range(200)])
        await rl.close()
        return sum(v.allowed for v in results)
    assert run(go()) == 10


def test_no_boundary_burst_on_real_redis():
    """The same GCRA guarantee, proven end-to-end against Redis: an instantaneous burst is capped at
    the limit, not double it."""
    async def go():
        rl = _limiter(max_requests=5, window=2)
        k = _key()
        allowed = 0
        for _ in range(20):
            if (await rl.check(k)).allowed:
                allowed += 1
        await rl.close()
        return allowed
    assert run(go()) == 5


def test_reset_unblocks_one_key():
    async def go():
        rl = _limiter(max_requests=1, window=60)
        a, b = _key(), _key()
        await rl.check(a); await rl.check(b)
        blocked = (await rl.check(a)).allowed
        await rl.reset(a)
        after_a = (await rl.check(a)).allowed
        after_b = (await rl.check(b)).allowed
        await rl.close()
        return blocked, after_a, after_b
    blocked, after_a, after_b = run(go())
    assert blocked is False and after_a is True and after_b is False


def test_degrades_to_LOCAL_when_redis_is_unreachable():
    """THE outage behaviour. Point at a dead Redis: the limiter keeps working (local fallback) and
    flags the verdict degraded — it does NOT 500 (closed) and does NOT wave everything through
    (open)."""
    from app.adapters.rate_limiter.redis import RedisRateLimiter
    rl = RedisRateLimiter("redis://127.0.0.1:1/0", 3, 60, degrade="local")  # port 1: nothing there
    k = _key()

    async def go():
        v = [await rl.check(k) for _ in range(5)]
        await rl.close()
        return v
    res = run(go())
    assert all(v.degraded for v in res), "an outage must be flagged, not silent"
    assert sum(v.allowed for v in res) == 3, "the local fallback still enforces the limit"


def test_degrade_closed_denies_on_outage():
    from app.adapters.rate_limiter.redis import RedisRateLimiter
    rl = RedisRateLimiter("redis://127.0.0.1:1/0", 3, 60, degrade="closed")
    v = run(rl.check(_key()))
    assert v.allowed is False and v.degraded is True


def test_degrade_open_allows_on_outage():
    from app.adapters.rate_limiter.redis import RedisRateLimiter
    rl = RedisRateLimiter("redis://127.0.0.1:1/0", 3, 60, degrade="open")
    v = run(rl.check(_key()))
    assert v.allowed is True and v.degraded is True


def test_bad_degrade_mode_fails_fast():
    from app.adapters.rate_limiter.redis import RedisRateLimiter
    from app.ports.errors import ConfigError
    with pytest.raises(ConfigError):
        RedisRateLimiter(_URL, 3, 60, degrade="halfway")
