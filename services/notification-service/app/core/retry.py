"""Retry policy — an outage must DELAY a message, not destroy it.

THE BUG THIS EXISTS FOR.

`notify()` sent inline. If the provider was down, the delivery was recorded `failed` and that was
the end of it: no retry, no queue, no dead-letter. A five-minute provider outage silently destroyed
every reminder sent during it, and the only trace was a row saying "failed" that nothing would ever
read again. The message was not delayed. It was gone, and nobody was told.

TWO THINGS THIS HAS TO GET RIGHT.

1. RETRYABLE IS NOT THE SAME AS FAILED.

   A provider timeout, a 502, a connection reset — those are the provider having a bad minute, and
   the message will go through if we simply ask again later.

   A malformed phone number, a missing template, an unknown channel — those will fail identically
   for ever. Retrying them is not resilience; it is a loop that burns provider quota, delays every
   real message queued behind it, and eventually dead-letters anyway. The only thing it achieves is
   spending money to arrive at the same answer.

   So a failure is CLASSIFIED, and only what can succeed is retried. What cannot is failed
   immediately and loudly.

2. BACKOFF WITHOUT JITTER IS A THUNDERING HERD.

   A provider comes back after an outage and every message that failed during it retries at exactly
   the same instant, because they all backed off by exactly the same doubling schedule. The provider
   falls over again — and this time WE knocked it over. Full jitter: the wait is a random point in
   [0, backoff], so a thousand retries spread out instead of arriving as one wave. Same discipline
   as the login lockout, for the same reason.
"""
from __future__ import annotations

import random

# Delivery states. `dead_letter` is DISTINCT from `failed`: it means "we tried, we kept trying, and
# we gave up" — and it must be findable, because a message that gave up and cannot be enumerated is
# indistinguishable from one that was silently lost.
QUEUED, SENDING, SENT, SUPPRESSED, FAILED, DEAD_LETTER = (
    "queued", "sending", "sent", "suppressed", "failed", "dead_letter")


class RetryPolicy:
    """How many times, how long, and — crucially — WHETHER AT ALL.

    Nothing here is hardcoded. What counts as a retryable error is a PLATFORM decision: one
    provider says "throttled", another says "rate_limited", a Saudi SMS gateway may say something
    we have never seen. The vocabulary is config, so a new provider is a new env value, not a code
    change.
    """

    def __init__(self, *, max_attempts: int, base_ms: int, max_ms: int,
                 retryable: tuple[str, ...], permanent: tuple[str, ...],
                 unknown_is_retryable: bool) -> None:
        if max_attempts < 1:
            raise ValueError("NOTIFY_MAX_ATTEMPTS must be >= 1 — zero attempts sends nothing")
        self.max_attempts = max_attempts
        self._base = base_ms
        self._max = max_ms
        self._retryable = tuple(r.lower() for r in retryable)
        self._permanent = tuple(p.lower() for p in permanent)
        self._unknown_is_retryable = unknown_is_retryable

    def is_retryable(self, error: str) -> bool:
        """Will asking again ever produce a different answer?

        PERMANENT wins over RETRYABLE. If an operator lists a token in both, the safe reading is
        "do not spend money on this again" — an over-eager retry costs real currency and delays
        every real message behind it, while an over-eager give-up costs one message that a human
        can resend.
        """
        e = (error or "").lower()
        if any(tok in e for tok in self._permanent):
            return False
        if any(tok in e for tok in self._retryable):
            return True
        # An error nobody classified. The default says what to do with the unknown, and it is a
        # deliberate choice rather than an accident of whichever branch happened to be first.
        return self._unknown_is_retryable

    def backoff_ms(self, attempt: int, *, rand: random.Random | None = None) -> int:
        """FULL JITTER: a random point in [0, exponential]. Never the exponential itself.

        `attempt` is 1-based. Without the jitter, every message that failed during an outage
        retries at the same instant the provider recovers, and we take it down again ourselves.
        """
        exp = min(self._base * (2 ** max(0, attempt - 1)), self._max)
        r = rand or random
        return int(r.uniform(0, exp))

    def next_state(self, attempt: int, error: str) -> str:
        """What this delivery becomes after a failed attempt.

        `failed` and `dead_letter` are both terminal, and the difference is the whole point:
        `failed` means we will never succeed, `dead_letter` means we ran out of patience. Collapsing
        them would hide which outages cost us messages.
        """
        if not self.is_retryable(error):
            return FAILED
        if attempt >= self.max_attempts:
            return DEAD_LETTER
        return QUEUED
