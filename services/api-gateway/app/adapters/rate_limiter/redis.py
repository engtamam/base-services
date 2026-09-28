"""Redis RateLimiter — GCRA sliding window, ATOMIC, and it DEGRADES rather than fails.

Two things this fixes that the old INCR/EXPIRE version got wrong, and that almost every naive
limiter gets wrong too.

ATOMIC (T5). `if count < max: incr` is two round trips, and under load N concurrent requests all
read the same count, all decide they are under the limit, and all increment. The limit is then
exceeded by exactly the concurrency. The GCRA update below is a single Lua script — Redis runs it
to completion with nothing interleaved, so the check and the store are indivisible.

DEGRADES, does not fail (T4). Making the Redis-outage behaviour a binary admin choice gives two
bad branches: fail-closed returns 500 (the limiter causes the outage it exists to prevent),
fail-open removes the limit entirely (so the limit is only as strong as Redis's uptime). Neither.
On an outage this falls back to a LOCAL per-pod limiter with the same policy: the budget becomes
coarser (`replicas x limit` instead of one shared budget) and it KEEPS WORKING. Coarse beats off,
and it beats down. The degradation is reported on the Verdict and logged, because a silent
degradation is a lie about the protection you have.
"""
from __future__ import annotations

import time
from typing import Any

from app.adapters.lazy import load
from app.adapters.rate_limiter.memory import MemoryRateLimiter
from app.ports.errors import ConfigError
from app.ports.rate_limiter import Verdict

# GCRA in Lua — runs atomically inside Redis. Mirrors sliding_window.check exactly, because the two
# MUST agree: a request must not be allowed by one and denied by the other depending on where it
# happened to run.
#   KEYS[1] = the key
#   ARGV = now_ms, limit, window_ms, cost
# Returns: {allowed(0/1), remaining, retry_after_ms, reset_ms}
_GCRA = """
local key      = KEYS[1]
local now      = tonumber(ARGV[1])
local limit    = tonumber(ARGV[2])
local window   = tonumber(ARGV[3])
local cost     = tonumber(ARGV[4])
local interval = window / limit

local tat = tonumber(redis.call('GET', key) or now)
if tat < now then tat = now end
local debt = tat - now
local headroom = window - cost * interval

if debt <= headroom then
  local new_tat = tat + cost * interval
  local ttl = math.ceil((new_tat - now) / 1000) + 1
  redis.call('SET', key, new_tat, 'PX', ttl * 1000)
  local remaining = math.floor((headroom - debt) / interval)
  return {1, remaining, 0, math.floor(debt + cost * interval)}
else
  return {0, 0, math.floor(debt - headroom) + 1, math.floor(debt)}
end
"""


# Reverse GCRA: move the TAT back by one request's interval, never before now. Atomic, and it only
# touches an existing key (a refund for a key that already expired is a no-op).
_REFUND = """
local key      = KEYS[1]
local now      = tonumber(ARGV[1])
local limit    = tonumber(ARGV[2])
local window   = tonumber(ARGV[3])
local cost     = tonumber(ARGV[4])
local interval = window / limit
local tat = redis.call('GET', key)
if not tat then return 0 end
local new_tat = tonumber(tat) - cost * interval
if new_tat < now then new_tat = now end
local ttl = math.ceil((new_tat - now) / 1000) + 1
redis.call('SET', key, new_tat, 'PX', ttl * 1000)
return 1
"""


class RedisRateLimiter:
    driver = "redis"

    def __init__(self, url: str, max_requests: int, window_seconds: int,
                 *, degrade: str = "local", max_connections: int = 64,
                 pool_timeout_s: float = 3.0, command_timeout_s: float = 1.0) -> None:
        if not url:
            raise ConfigError("RATELIMIT_URL is required for the redis driver")
        redis = load("redis.asyncio", "redis")
        # A BLOCKING pool, deliberately. The limiter runs on every request, so a burst asks for many
        # connections at once. The default pool RAISES when it runs out — and that error, caught
        # below, would look exactly like an outage and degrade to the local fallback, handing the
        # burst a SECOND independent budget (the limit would silently double under load, which is
        # the precise failure a rate limiter must never have). A blocking pool makes the surplus
        # requests QUEUE for a connection instead, so they still hit the one shared counter. Redis
        # being merely busy is backpressure, not an outage.
        #
        # `pool_timeout_s` only bounds the WAIT FOR A FREE CONNECTION out of the pool — not the socket.
        # Without `socket_timeout`/`socket_connect_timeout` the underlying connection blocks FOREVER on
        # a connect or read, so when Redis is unreachable (not merely busy) the GCRA call in check()
        # hangs indefinitely and the `except`-guarded degrade path below never runs — the request
        # instead dies at the gateway's upstream timeout as a 504, the exact failure this design's
        # "degrade, don't fail" fallback exists to prevent. So bound the socket too, and keep it SHORT:
        # the point of the fallback is to fail fast into it, not to wait as long as possible.
        pool = redis.BlockingConnectionPool.from_url(
            url, decode_responses=True, max_connections=max_connections, timeout=pool_timeout_s,
            socket_timeout=command_timeout_s, socket_connect_timeout=command_timeout_s)
        self._client: Any = redis.Redis(connection_pool=pool)
        self._max = max(1, max_requests)
        self._window_ms = max(1, window_seconds) * 1000
        self._script = self._client.register_script(_GCRA)
        self._refund_script = self._client.register_script(_REFUND)
        # DEGRADE: what to do when Redis cannot be reached.
        #   local  (default) answer from a per-pod fallback with the same policy. Coarser, alive.
        #   closed deny everything (fail secure, but the limiter becomes the outage).
        #   open   allow everything (fail available, but the limit is gone).
        if degrade not in ("local", "closed", "open"):
            raise ConfigError(f"unknown RATELIMIT_DEGRADE {degrade!r} — allowed: local, closed, open")
        self._degrade = degrade
        self._fallback = MemoryRateLimiter(max_requests, window_seconds)

    def _k(self, key: str) -> str:
        return f"ratelimit:{key}"

    async def check(self, key: str, *, cost: int = 1,
                    limit: int | None = None, window_s: int | None = None) -> Verdict:
        lim = limit if limit is not None else self._max
        win_ms = (window_s * 1000) if window_s is not None else self._window_ms
        now = int(time.time() * 1000)
        try:
            allowed, remaining, retry_ms, reset_ms = await self._script(
                keys=[self._k(key)], args=[now, lim, win_ms, cost])
            return Verdict(
                allowed=bool(allowed), limit=lim, remaining=int(remaining),
                retry_after_s=(int(retry_ms) + 999) // 1000,
                reset_s=(int(reset_ms) + 999) // 1000)
        except Exception:
            return await self._degraded(key, cost=cost, limit=lim, window_s=win_ms // 1000)

    async def _degraded(self, key: str, *, cost: int, limit: int, window_s: int) -> Verdict:
        if self._degrade == "open":
            return Verdict(True, limit, limit, 0, 0, degraded=True)
        if self._degrade == "closed":
            return Verdict(False, limit, 0, window_s, window_s, degraded=True)
        v = await self._fallback.check(key, cost=cost, limit=limit, window_s=window_s)
        # Same verdict, flagged degraded so the caller can log it and /metrics can count it. The
        # limit is now per-pod; that is the honest price of surviving the outage.
        return Verdict(v.allowed, v.limit, v.remaining, v.retry_after_s, v.reset_s, degraded=True)

    async def refund(self, key: str, *, cost: int = 1,
                     limit: int | None = None, window_s: int | None = None) -> None:
        """Return budget to a request that should not have counted (T6). Atomic, and a no-op if the
        key is gone. A refund failure is swallowed: over-counting one login attempt is a nuisance,
        not a security hole, and it must never turn into a 500 on the response path."""
        lim = limit if limit is not None else self._max
        win_ms = (window_s * 1000) if window_s is not None else self._window_ms
        now = int(time.time() * 1000)
        try:
            await self._refund_script(keys=[self._k(key)], args=[now, lim, win_ms, cost])
        except Exception:
            await self._fallback.refund(key, cost=cost, limit=lim, window_s=win_ms // 1000)

    async def allow(self, key: str) -> bool:
        return (await self.check(key)).allowed

    async def reset(self, key: str) -> None:
        try:
            await self._client.delete(self._k(key))
        except Exception:
            pass
        await self._fallback.reset(key)

    async def health(self) -> bool:
        try:
            return bool(await self._client.ping())
        except Exception:
            return False

    async def close(self) -> None:
        await self._client.aclose()
