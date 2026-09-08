"""Null inbound source — the safe default when no feed is wired.

Returns no messages and never advances a cursor, so a deployment that enables the ingest schedule
without pointing it at a feed does nothing rather than erroring — the same zero-infra stance as the
null notification sender.
"""
from __future__ import annotations

from app.core.inbound_bridge import InboundMessage


class NullInboundSource:
    driver = "null"

    async def read(self, *, after: str, limit: int) -> tuple[list[InboundMessage], str | None]:
        return [], None
