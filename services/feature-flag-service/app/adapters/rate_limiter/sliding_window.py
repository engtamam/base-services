"""GCRA sliding-window rate limiting — the algorithm, pure and testable.

WHY NOT A FIXED WINDOW.

A fixed window (INCR + EXPIRE — what this service, and `ammar`, and almost every naive limiter do)
allows **twice** the configured budget across a window boundary: spend the whole budget in the last
millisecond of one window, and the whole budget again in the first millisecond of the next. Two
bursts, back to back, adding up to 2× — and the per-window graph looks perfectly innocent while the
origin takes double the load you thought you had capped. See phase-32 T3.

WHAT GCRA DOES INSTEAD.

The Generic Cell Rate Algorithm stores ONE number per key — a "theoretical arrival time" (TAT) — and
treats the limit as a continuously-decaying allowance rather than a bucket that refills all at once
on a clock edge. There is no boundary to straddle, because there is no boundary. It is a sliding
window that costs a single stored value, which is exactly what makes it cheap enough for the hot
path and for one Redis key.

It is deliberately a pure function of (state, now, cost). No clock is read inside it — the caller
passes `now_ms`, so a test drives the exact boundary-straddle attack with a fake clock and no sleep.
"""
from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class Decision:
    allowed: bool
    remaining: int          # how many more requests are allowed right now
    retry_after_ms: int     # 0 when allowed; otherwise when to try again
    new_tat: float          # the state to store back (the caller persists it)
    reset_ms: int           # when the limit fully clears if no further requests arrive


def check(tat: float, now_ms: float, *, limit: int, window_ms: int, cost: int = 1) -> Decision:
    """One request against the GCRA state. Returns the decision AND the state to store.

    `tat` is the stored theoretical arrival time (0.0 for a key never seen). `cost` lets an
    expensive endpoint consume more of the budget than a cheap one — a search that fans out to five
    services should not be one token, the same as a health check.
    """
    if limit < 1:
        raise ValueError("limit must be >= 1")
    if window_ms < 1:
        raise ValueError("window_ms must be >= 1")
    cost = max(1, cost)

    # Time the limit gives back per single request. `limit` requests worth of interval == one
    # window, which is what makes the burst capacity exactly `limit`.
    interval = window_ms / limit

    tat = max(tat, now_ms)
    new_tat = tat + cost * interval

    # You may run at most `window_ms` ahead of now — i.e. `limit` intervals of credit. Past that,
    # you are asking for budget the window has not yet handed back.
    allow_at = new_tat - window_ms

    if allow_at <= now_ms:
        # Allowed. How many MORE fit before we hit the ceiling: the remaining head-room, in
        # whole requests. This is what the RateLimit-Remaining header should report.
        head_room_ms = (now_ms + window_ms) - new_tat
        remaining = int(head_room_ms // interval)
        reset_ms = int(new_tat - now_ms)          # when this key's credit fully clears
        return Decision(True, remaining, 0, new_tat, reset_ms)

    # Denied. The state is NOT advanced (a rejected request must not push the TAT further out, or a
    # client hammering a throttled key would extend its own lockout forever). retry_after is exactly
    # when the next single request of this cost would be admitted.
    retry_after = int(allow_at - now_ms) + 1
    reset_ms = int(tat - now_ms)
    return Decision(False, 0, retry_after, tat, reset_ms)


def refund(tat: float, now_ms: float, *, limit: int, window_ms: int, cost: int = 1) -> float:
    """Give back the budget a request consumed, returning the TAT to store.

    This is what makes "count only failed attempts" possible (T6, ammar's idea). The limiter must
    decide BEFORE the handler runs — so it counts every request up front, and then, once the
    response status is known, a request that should not have counted (a correct password on an
    account pending approval — a 403, not a 401) is REFUNDED here.

    GCRA is a timestamp, so a refund is simply moving the TAT back by one request's interval, never
    earlier than now (you cannot bank credit for the future by succeeding).
    """
    interval = window_ms / max(1, limit)
    return max(now_ms, tat - max(1, cost) * interval)
