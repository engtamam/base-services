"""EventBus port — publish/subscribe for domain events.

    DELIVERY IS AT-MOST-ONCE. BEST EFFORT. THERE IS NO GUARANTEE.

Say it plainly, because a consumer that assumes otherwise is already broken and does not know it.

What actually happens: `publish()` fans out to whatever handlers are subscribed AT THAT MOMENT and
swallows their exceptions, so one bad subscriber cannot break the publisher. There is no outbox, no
durable subscription, no retry, no dead-letter, and no replay. A subscriber that is restarting,
disconnected, or simply not deployed yet does not receive the event, and nothing anywhere records
that it missed one.

THE RULE THIS IMPOSES ON EVERY CONSUMER:

    An event is a HINT that makes a reconciliation faster.
    It is NEVER the only way a fact can be learned.

If the only way a service can discover that a tenant was deleted is by receiving `tenant.deleted`,
then a pod restart at the wrong second means that service believes the tenant still exists — for
ever. Not for a while. For ever, because nothing will ever tell it again. And a peer that fails
open on "unknown tenant" (a reasonable choice: an unknown tenant is usually an onboarding gap, not
a punishment) will then keep accepting writes to a company that no longer exists.

So every fact a consumer depends on must ALSO be readable: a tombstone, a status field, an endpoint
it can poll. A read is self-healing. A missed event is wrong for ever.

Upgrading this port to at-least-once (an outbox + durable subscriptions + idempotent consumers) is
a real piece of work and a good one. Until it is done, this docstring is the contract, and it is
the contract precisely because somebody designed against a guarantee we never had.
"""
from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any, Protocol, runtime_checkable


@dataclass(frozen=True)
class Event:
    type: str
    payload: dict[str, Any]
    id: str = ""
    correlation_id: str = ""
    ts: str = ""
    meta: dict[str, Any] = field(default_factory=dict)


Handler = Callable[[Event], Awaitable[None]]


@runtime_checkable
class EventBus(Protocol):
    driver: str

    async def publish(self, topic: str, event: Event) -> None: ...

    async def subscribe(self, topic: str, handler: Handler) -> None: ...

    async def health(self) -> bool: ...

    async def close(self) -> None: ...
