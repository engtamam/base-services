"""The redis limiter must FAIL FAST into its degrade path when Redis is unreachable.

The bug: the pool was built with only a pool-wait timeout, never a socket timeout, so a GCRA call
against an unreachable Redis blocked forever on the TCP connect/read. The `except`-guarded degrade
path (the module's whole "degrade, don't fail" reason for being) never ran, and every gated request
died at the gateway's upstream timeout as a 504. The fix passes socket_timeout / socket_connect_timeout
through, so an outage raises promptly and hits the fallback.

Needs the `redis` package to build the client (skipped where the s3/redis extra is absent). Uses an
unreachable local port — nothing must be listening — so no real Redis is required.
"""
from __future__ import annotations

import asyncio
import time

import pytest

pytest.importorskip("redis", reason="redis extra not installed")

from app.adapters.rate_limiter.redis import RedisRateLimiter   # noqa: E402

# A port nothing listens on: a connect here refuses/times out fast, standing in for an unreachable Redis.
_DEAD_URL = "redis://127.0.0.1:6390/0"


def test_socket_timeouts_are_passed_to_the_connection():
    rl = RedisRateLimiter(_DEAD_URL, 10, 60, command_timeout_s=0.5)
    ck = rl._client.connection_pool.connection_kwargs
    # Both must be set, or a connect/read can still block forever — pool_timeout alone does not bound it.
    assert ck.get("socket_timeout") == 0.5
    assert ck.get("socket_connect_timeout") == 0.5


def test_unreachable_redis_degrades_promptly_instead_of_hanging():
    async def go():
        rl = RedisRateLimiter(_DEAD_URL, 5, 60, degrade="local", command_timeout_s=0.5)
        t0 = time.monotonic()
        verdict = await rl.check("k")           # before the fix this never returned — it hung
        elapsed = time.monotonic() - t0
        await rl.close()
        return verdict.allowed, elapsed

    allowed, elapsed = asyncio.run(go())
    # Degraded to the per-pod fallback (local) → the first request is allowed, and it returned fast:
    # a generous bound well under any gateway upstream timeout, proving the socket timeout fired.
    assert allowed is True
    assert elapsed < 5.0, f"degrade took {elapsed:.2f}s — the socket timeout did not bound the call"
