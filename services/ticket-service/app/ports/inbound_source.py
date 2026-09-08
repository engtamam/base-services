"""InboundSource port — a feed of normalised inbound channel messages the bridge turns into tickets.

Generic: the bridge (core/inbound_bridge.py) consumes InboundMessage and knows nothing about the
channel. An adapter reads ONE source (e.g. notification-service's durable WhatsApp inbound feed) and
normalises it. `read` is a forward, cursor-paged catch-up read — the same reconciliation contract the
rest of the platform uses — so the scheduler drives it, persisting the returned cursor as a watermark.
"""
from __future__ import annotations

from typing import Protocol, runtime_checkable

from app.core.inbound_bridge import InboundMessage


class InboundSourceUnavailable(Exception):
    """The feed could not be read — the ingest run stops cleanly and the cursor is NOT advanced, so a
    later run retries from the same watermark rather than skipping messages."""


@runtime_checkable
class InboundSource(Protocol):
    driver: str

    async def read(self, *, after: str, limit: int) -> tuple[list[InboundMessage], str | None]:
        """Messages forward from `after` (a watermark; "" from the start), oldest first, bounded by
        `limit`. Returns (messages, next_cursor); next_cursor is None when caught up. Raise
        InboundSourceUnavailable on a read failure — never return [] for an error (that would advance
        the watermark past unread messages)."""
        ...
