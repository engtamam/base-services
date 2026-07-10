"""Retry backoff — full jitter, and it HONOURS the provider's `Retry-After`.

Providers tell you exactly when to come back (429/503 + `Retry-After`). Ignoring that header
and applying your own exponential curve is how an integration gets an app suspended. When
the provider speaks, we listen; otherwise we use full jitter (plain 2ⁿ synchronises every
failed connection into one thundering herd).
"""
from __future__ import annotations

import hashlib


def _unit(seed: str) -> float:
    return int.from_bytes(hashlib.sha256(seed.encode()).digest()[:8], "big") / float(1 << 64)


def parse_retry_after(value: str | None) -> float | None:
    """`Retry-After: 120` (delta-seconds). HTTP-date form is ignored (rare, clock-skew prone)."""
    if not value:
        return None
    try:
        secs = float(str(value).strip())
    except (TypeError, ValueError):
        return None
    return secs if secs >= 0 else None


def next_delay(attempt: int, *, base_seconds: int, max_seconds: int, max_attempts: int,
               seed: str = "", jitter: bool = True, retry_after: float | None = None) -> float | None:
    """Seconds until the next attempt, or None when exhausted (→ dead-letter, never dropped)."""
    n = max(0, int(attempt))
    if n >= max_attempts:
        return None
    if retry_after is not None:                 # the provider told us — obey it (capped)
        return min(float(retry_after), float(max_seconds))
    ceiling = min(base_seconds * (2 ** n), max_seconds)
    return _unit(f"{seed}:{n}") * ceiling if jitter else float(ceiling)
