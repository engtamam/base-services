"""In-memory RateLimiter — fixed-window per key. Zero infra."""
from __future__ import annotations

import time


class MemoryRateLimiter:
    driver = "memory"

    def __init__(self, max_requests: int, window_seconds: int) -> None:
        self._max = max(1, max_requests)
        self._window = max(1, window_seconds)
        # key -> (window_start_monotonic, count)
        self._buckets: dict[str, tuple[float, int]] = {}

    async def allow(self, key: str) -> bool:
        now = time.monotonic()
        start, count = self._buckets.get(key, (now, 0))
        if now - start >= self._window:
            start, count = now, 0  # new window
        if count >= self._max:
            self._buckets[key] = (start, count)
            return False
        self._buckets[key] = (start, count + 1)
        return True

    async def reset(self, key: str) -> None:
        self._buckets.pop(key, None)

    async def health(self) -> bool:
        return True

    async def close(self) -> None:
        self._buckets.clear()
