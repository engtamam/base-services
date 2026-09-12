"""In-memory Cache with TTL — zero infra.

BOUNDED: the gateway keys the response cache by method+path+query, and an anonymous GET with
attacker-varied query strings would otherwise grow the dict without limit (a memory DoS, since entries
were only evicted lazily on a re-read of the SAME key). A hard entry cap with LRU eviction and an
expired-sweep on insert keep it bounded (api-gateway-r2-02)."""
from __future__ import annotations

import time
from collections import OrderedDict

_DEFAULT_MAX_ENTRIES = 10_000


class MemoryCache:
    driver = "memory"

    def __init__(self, max_entries: int = _DEFAULT_MAX_ENTRIES) -> None:
        # key -> (value, expires_at_monotonic | None); insertion/most-recent order for LRU eviction
        self._data: "OrderedDict[str, tuple[str, float | None]]" = OrderedDict()
        self._max = max(1, int(max_entries))

    def _expired(self, key: str) -> bool:
        entry = self._data.get(key)
        if entry is None:
            return True
        _, exp = entry
        if exp is not None and time.monotonic() >= exp:
            self._data.pop(key, None)
            return True
        return False

    async def get(self, key: str) -> str | None:
        if self._expired(key):
            return None
        self._data.move_to_end(key)                 # LRU: reading marks it most-recently-used
        return self._data[key][0]

    async def set(self, key: str, value: str, ttl_seconds: int | None = None) -> None:
        now = time.monotonic()
        # Opportunistic expired-sweep (bounded work) so dead keys don't hold slots against the cap.
        for k in [k for k, (_, e) in list(self._data.items())[:64] if e is not None and now >= e]:
            self._data.pop(k, None)
        exp = now + ttl_seconds if ttl_seconds else None
        self._data[key] = (value, exp)
        self._data.move_to_end(key)
        while len(self._data) > self._max:          # hard cap: evict the least-recently-used
            self._data.popitem(last=False)

    async def delete(self, key: str) -> bool:
        return self._data.pop(key, None) is not None

    async def health(self) -> bool:
        return True

    async def close(self) -> None:
        self._data.clear()
