"""Regression: WhatsAppNumberIndex.bind is atomic first-wins (safety:atomic-cas).

Two tenants binding the same phone number concurrently must not both win — the second would silently
hijack the incumbent's inbound routing.
"""
from __future__ import annotations

import asyncio

from app.adapters.datastore.memory import MemoryDataStore
from app.core.stores import WhatsAppNumberIndex


def run(c):
    return asyncio.run(c)


class _YieldStore(MemoryDataStore):
    async def get(self, c, k):
        await asyncio.sleep(0)
        return await super().get(c, k)


def test_concurrent_bind_first_wins():
    idx = WhatsAppNumberIndex(_YieldStore())

    async def _race():
        return await asyncio.gather(
            idx.bind("wa_123", "tenant_a"),
            idx.bind("wa_123", "tenant_b"))

    outcomes = run(_race())
    assert outcomes.count(WhatsAppNumberIndex.BOUND) == 1          # exactly one tenant bound it
    assert outcomes.count(WhatsAppNumberIndex.CONFLICT) == 1       # the other refused (pre-fix: hijack)
    # and the winner owns it durably
    owner = run(idx.tenant_of("wa_123"))
    assert owner in ("tenant_a", "tenant_b")
