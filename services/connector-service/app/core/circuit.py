"""Per-connection circuit breaker — a dead provider must not consume the sync workers.

Pure functions over the connection's stored state, so nothing is lost across replicas.
  closed → sync normally · open → skip (runs stay queued) · half_open → one probe
"""
from __future__ import annotations

CLOSED, OPEN, HALF_OPEN = "closed", "open", "half_open"


def state_now(*, circuit_state: str, opened_at: float, now: float, reset_seconds: int) -> str:
    if circuit_state == OPEN and now - float(opened_at or 0) >= reset_seconds:
        return HALF_OPEN
    return circuit_state or CLOSED


def can_attempt(state: str) -> bool:
    return state in (CLOSED, HALF_OPEN)


def on_success(**_) -> dict:
    return {"circuit_state": CLOSED, "consecutive_failures": 0, "opened_at": 0.0}


def on_failure(*, consecutive_failures: int, state: str, now: float, threshold: int) -> dict:
    failures = int(consecutive_failures) + 1
    if state == HALF_OPEN or failures >= threshold:      # a failed probe re-opens at once
        return {"circuit_state": OPEN, "consecutive_failures": failures, "opened_at": now}
    return {"circuit_state": CLOSED, "consecutive_failures": failures, "opened_at": 0.0}
