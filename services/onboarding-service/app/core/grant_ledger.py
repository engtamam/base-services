"""Our record of every grant issued, keyed by its uniqueness key.

Separate from LocalFacts on purpose: this is keyed by the ANTI-ABUSE key (a verified identity),
while LocalFacts is keyed by tenant/subject. That difference is the whole point — a tenant can be
deleted and remade, and this ledger must still recognise the identity behind it.

Like verification-service's identity_seen, these rows survive erasure and for the same reason: the
key is a one-way hash naming nobody, and deleting it would re-open the fraud it exists to close.
The erasure ack states that rather than exercising it quietly.
"""
from __future__ import annotations

from typing import Any

_LEDGER = "grant_ledger"


class GrantLedger:
    def __init__(self, store, clock) -> None:
        self._store = store
        self._clock = clock

    async def find(self, key: str) -> dict[str, Any] | None:
        return await self._store.get(_LEDGER, key)

    async def record(self, key: str, *, kind: str, tenant: str, subject: str,
                     reference: str) -> None:
        # Note what is NOT stored: the subject. The row must outlive the person's account, and
        # keeping their id would make it personal data we could not justify retaining.
        await self._store.put(_LEDGER, key, {
            "id": key, "kind": kind, "reference": reference, "at": self._clock.now()})
