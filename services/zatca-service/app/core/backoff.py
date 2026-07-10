"""Escalating retry backoff — pure. next = min(base·2^attempt, max); cap → stop."""
from __future__ import annotations


def compute_retry_schedule(attempt: int, *, base_ms: int, max_ms: int,
                           max_attempts: int) -> dict:
    """attempt is the count of prior attempts (0-based). Returns
    {next_delay_ms, capped}. When capped, next_delay_ms is None (needs-attention)."""
    try:
        n = int(attempt)
    except (TypeError, ValueError):
        n = 0
    if n < 0:
        n = 0
    if n >= max_attempts:
        return {"next_delay_ms": None, "capped": True}
    delay = min(base_ms * (2 ** n), max_ms)
    return {"next_delay_ms": delay, "capped": False}
