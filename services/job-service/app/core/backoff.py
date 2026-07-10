"""Retry backoff with FULL JITTER — deterministic per (job, attempt), no global RNG.

Plain base·2ⁿ makes every job that failed in one outage retry at the same instant
(thundering herd). Full jitter spreads them across the window.
"""
from __future__ import annotations

import hashlib


def _unit(seed: str) -> float:
    return int.from_bytes(hashlib.sha256(seed.encode()).digest()[:8], "big") / float(1 << 64)


def next_delay(attempt: int, *, base_seconds: int, max_seconds: int, max_attempts: int,
               seed: str = "", jitter: bool = True) -> float | None:
    """Seconds until the next attempt, or None when exhausted (→ dead-letter, never dropped)."""
    n = max(0, int(attempt))
    if n >= max_attempts:
        return None
    ceiling = min(base_seconds * (2 ** n), max_seconds)
    return _unit(f"{seed}:{n}") * ceiling if jitter else float(ceiling)
