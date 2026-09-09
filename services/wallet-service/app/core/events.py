"""The domain events the wallet publishes.

WHAT AN EVENT IS HERE, AND WHAT IT IS NOT. The bus is at-most-once and best effort — no outbox, no
retry, no replay (see `ports/event_bus.py`). An event therefore tells any consumer that something
happened so they can react WITHOUT this service knowing they exist. It is a HINT.

It is NEVER the only way a fact can be learned. A subscriber that is restarting when
`wallet.balance_low` fires never receives it and nothing records that it missed one — so a platform
whose only warning is that event warns nobody, for ever, and the customer discovers the problem as a
402 in the middle of a job. That is why the low-balance state is ALSO a field on the balance read
(`low_balance`, `low_balance_threshold`): a read is self-healing, a missed event is wrong for ever.

AN EVENT IS AN `Event`, NEVER A RAW DICT. The port and every adapter type it as `Event`; a dict
reaches a subscriber that does `event.type`, raises inside the handler, and the bus swallows it —
so the publisher looks successful while the consumer never acts, which is the hardest shape of
failure to see from either side.

PUBLISHING NEVER BREAKS THE THING THAT HAPPENED, by default. The money has already moved by the
time these fire, and refusing a spend because a broker is unreachable turns a degraded notification
path into an outage. `EVENT_PUBLISH_FAILURE=fail_closed` is available for platforms that would
rather refuse than proceed unobserved — it is not the default, because here the alternative to a
missed hint is a customer who cannot spend money they have.
"""
from __future__ import annotations

import logging
import uuid
from typing import Any

from app.ports.event_bus import Event

_log = logging.getLogger(__name__)

# Declared here and in the manifest, and the two must agree — an event a consumer cannot discover
# is one nobody subscribes to, which is operationally the same as not publishing it.
WALLET_CREDITED = "wallet.credited"
WALLET_SPENT = "wallet.spent"
BALANCE_LOW = "wallet.balance_low"
BALANCE_RECOVERED = "wallet.balance_recovered"
# Grant lifecycle — a consumer (e.g. notification-service) tells the customer BEFORE credit lapses
# (the SAMA notice principle + plain good UX) and when it did.
GRANT_EXPIRING = "wallet.grant_expiring"      # pre-expiry notice (once, notice_days before)
GRANT_EXPIRED = "wallet.grant_expired"        # remainder forfeited (clawback) or made dormant (expire)
GRANT_RECLAIMED = "wallet.grant_reclaimed"    # a dormant grant restored to spendable

EMITTED = (WALLET_CREDITED, WALLET_SPENT, BALANCE_LOW, BALANCE_RECOVERED,
           GRANT_EXPIRING, GRANT_EXPIRED, GRANT_RECLAIMED)

# The two failure policies, named rather than spelled as a bool at each call site.
FAIL_OPEN = "fail_open"
FAIL_CLOSED = "fail_closed"


class Emitter:
    """Publishes domain events, and decides what happens when the bus cannot be reached."""

    def __init__(self, bus, *, fail_closed: bool = False, clock=None) -> None:
        self._bus = bus                  # None when no bus is wired: publishing is then a no-op
        self._fail_closed = bool(fail_closed)
        self._clock = clock

    async def emit(self, event_type: str, payload: dict[str, Any], *,
                   correlation_id: str = "") -> None:
        if self._bus is None:
            return
        if event_type not in EMITTED:
            # An undeclared event is one no consumer can discover from the manifest, so it reaches
            # nobody by design. Refusing here keeps the declaration and the code in step rather
            # than letting them drift until somebody reads both.
            raise ValueError(
                f"{event_type!r} is not declared in EMITTED — declare it there and in the "
                f"manifest, or no consumer can discover it")
        event = Event(
            type=event_type,
            payload=payload,
            id=str(uuid.uuid4()),
            correlation_id=correlation_id,
            ts=str(self._clock.now()) if self._clock else "",
        )
        try:
            await self._bus.publish(event_type, event)
        except Exception as exc:  # noqa: BLE001 — any broker failure, and there are many
            if self._fail_closed:
                raise
            # The money already moved. Failing the spend because a broker is unreachable turns a
            # degraded notification path into an outage for whoever was mid-task.
            _log.warning("event_publish_failed",
                         extra={"event": event_type, "error": type(exc).__name__})


__all__ = ["EMITTED", "Emitter", "WALLET_CREDITED", "WALLET_SPENT", "BALANCE_LOW",
           "BALANCE_RECOVERED", "FAIL_OPEN", "FAIL_CLOSED"]
