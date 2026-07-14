"""RateLimiter port — throttle by key (per IP / subject / tenant / route).

Two entry points, one for each kind of caller:

  allow(key) -> bool          the simple gate every existing middleware already uses.
  check(key, ...) -> Verdict  the full answer: allowed, how many remain, and when to retry —
                              everything a `Retry-After` / `RateLimit-*` response needs.

`allow` is `check` with the detail thrown away, so there is exactly one limiting algorithm and the
two can never disagree.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol, runtime_checkable


@dataclass(frozen=True)
class Verdict:
    allowed: bool
    limit: int              # the ceiling that applied
    remaining: int          # requests still available in this window
    retry_after_s: int      # 0 when allowed; seconds until the next request would pass
    reset_s: int            # seconds until the key's budget fully clears
    degraded: bool = False  # True when answered by the LOCAL fallback (shared store was down)


@runtime_checkable
class RateLimiter(Protocol):
    driver: str

    async def check(self, key: str, *, cost: int = 1,
                    limit: int | None = None, window_s: int | None = None) -> "Verdict":
        """Decide, and report everything a caller needs to answer well.

        `cost` weights an expensive endpoint above a cheap one. `limit`/`window_s` override the
        driver defaults for THIS call, so one limiter instance can serve several route classes
        (a strict `login` budget and a loose `api` budget) without a second connection.
        """
        ...

    async def allow(self, key: str) -> bool:
        """True if allowed. The thin gate — `check(key).allowed`, no more."""
        ...

    async def refund(self, key: str, *, cost: int = 1,
                     limit: int | None = None, window_s: int | None = None) -> None:
        """Give back the budget a request consumed — for "count only failed attempts" (T6).

        The limiter must decide before the handler runs, so it counts every request up front; a
        request whose final status should not have counted is refunded here. A refund is best-effort
        and never raises: over-counting one attempt is a nuisance, not a security hole.
        """
        ...

    async def reset(self, key: str) -> None:
        """Clear ONE key. The operator's unblock — never a flush of everything."""
        ...

    async def health(self) -> bool: ...

    async def close(self) -> None: ...
