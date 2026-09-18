"""In-memory RateLimiter — GCRA sliding window, per pod. Zero infra.

Per POD is the catch, and it is honest about it: run more than one replica and each keeps its own
counter, so the real budget is `replicas x limit`. That is fine for a single-instance dev box and a
disaster silently applied to a fleet — which is why the chart refuses `replicas > 1` with this
driver (phase-32 T2), and why the redis driver exists.

What it is NOT is a fixed window. The shared algorithm (sliding_window.check) means this driver and
the redis driver make the same decision for the same inputs, so a deployment that starts on memory
and moves to redis does not change behaviour, only scope.

It also doubles as the LOCAL FALLBACK the redis driver degrades to when Redis is down — same code,
so a degraded limit is a coarser limit, never a different one.
"""
from __future__ import annotations

import asyncio
import time

from app.adapters.rate_limiter import sliding_window as sw
from app.ports.rate_limiter import Verdict


class MemoryRateLimiter:
    driver = "memory"

    def __init__(self, max_requests: int, window_seconds: int) -> None:
        self._max = max(1, max_requests)
        self._window_ms = max(1, window_seconds) * 1000
        self._tats: dict[str, float] = {}
        # The check-and-store must be ONE critical section. Without it two coroutines read the same
        # TAT, both decide "allowed", and both store — the read-then-write race, T5. asyncio.Lock
        # serialises them; the section is a few microseconds of pure arithmetic, so it does not
        # become the bottleneck it is protecting against.
        self._lock = asyncio.Lock()

    def _now(self) -> float:
        return time.monotonic() * 1000.0

    async def check(self, key: str, *, cost: int = 1,
                    limit: int | None = None, window_s: int | None = None) -> Verdict:
        lim = limit if limit is not None else self._max
        win_ms = (window_s * 1000) if window_s is not None else self._window_ms
        now = self._now()
        async with self._lock:
            d = sw.check(self._tats.get(key, 0.0), now, limit=lim, window_ms=win_ms, cost=cost)
            if d.allowed:
                self._tats[key] = d.new_tat
        return Verdict(
            allowed=d.allowed, limit=lim, remaining=d.remaining,
            retry_after_s=(d.retry_after_ms + 999) // 1000,     # round UP: never advertise too soon
            reset_s=(d.reset_ms + 999) // 1000)

    async def allow(self, key: str) -> bool:
        return (await self.check(key)).allowed

    async def refund(self, key: str, *, cost: int = 1,
                     limit: int | None = None, window_s: int | None = None) -> None:
        lim = limit if limit is not None else self._max
        win_ms = (window_s * 1000) if window_s is not None else self._window_ms
        now = self._now()
        async with self._lock:
            cur = self._tats.get(key)
            if cur is None:
                return
            self._tats[key] = sw.refund(cur, now, limit=lim, window_ms=win_ms, cost=cost)

    async def reset(self, key: str) -> None:
        async with self._lock:
            self._tats.pop(key, None)

    async def health(self) -> bool:
        return True

    async def close(self) -> None:
        self._tats.clear()
