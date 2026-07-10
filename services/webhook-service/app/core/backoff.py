"""Retry backoff with FULL JITTER.

Plain `base·2ⁿ` synchronises every delivery that failed in the same outage: they all wake
at the same instant and hammer the recovering endpoint (thundering herd). Full jitter —
`uniform(0, min(base·2ⁿ, cap))` — spreads them across the whole window.

Deterministic per (delivery_id, attempt) so a retry schedule is reproducible and testable:
the jitter is drawn from a hash, not a global RNG.
"""
from __future__ import annotations

import hashlib


def _unit(seed: str) -> float:
    """A stable [0,1) drawn from the seed — no global RNG, reproducible in tests."""
    digest = hashlib.sha256(seed.encode()).digest()
    return int.from_bytes(digest[:8], "big") / float(1 << 64)


def next_delay(attempt: int, *, base_seconds: int, max_seconds: int, max_attempts: int,
               seed: str = "", jitter: bool = True) -> float | None:
    """Seconds until the next attempt, or None when exhausted (→ dead-letter, never dropped)."""
    n = max(0, int(attempt))
    if n >= max_attempts:
        return None
    ceiling = min(base_seconds * (2 ** n), max_seconds)
    if not jitter:
        return float(ceiling)
    return _unit(f"{seed}:{n}") * ceiling               # full jitter: uniform(0, ceiling)
