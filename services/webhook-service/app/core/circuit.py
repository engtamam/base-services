"""Per-endpoint circuit breaker — a dead customer endpoint must not eat the worker pool.

  closed     → deliver normally
  open       → skip delivery (deliveries stay QUEUED, never dropped) until reset elapses
  half_open  → let exactly one probe through; success closes, failure re-opens

Pure functions over the endpoint's stored state, so there is no in-process state to lose
across replicas.
"""
from __future__ import annotations

CLOSED, OPEN, HALF_OPEN = "closed", "open", "half_open"


def state_now(*, circuit_state: str, opened_at: float, now: float, reset_seconds: int) -> str:
    """An `open` circuit becomes `half_open` once the reset window has elapsed."""
    if circuit_state == OPEN and now - float(opened_at or 0) >= reset_seconds:
        return HALF_OPEN
    return circuit_state or CLOSED


def can_attempt(state: str) -> bool:
    return state in (CLOSED, HALF_OPEN)


def on_success(*, consecutive_failures: int) -> dict:
    return {"circuit_state": CLOSED, "consecutive_failures": 0, "opened_at": 0.0}


def on_failure(*, consecutive_failures: int, state: str, now: float, threshold: int) -> dict:
    failures = int(consecutive_failures) + 1
    # a failed half-open probe re-opens immediately, regardless of the counter
    if state == HALF_OPEN or failures >= threshold:
        return {"circuit_state": OPEN, "consecutive_failures": failures, "opened_at": now}
    return {"circuit_state": CLOSED, "consecutive_failures": failures, "opened_at": 0.0}
