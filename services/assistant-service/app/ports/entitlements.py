"""EntitlementResolver port — what a tenant currently HOLDS, resolved server-side, never claimed.

KB visibility has two independent axes. `KbEntry.requires` gates on the caller's ROLE capability,
which rides in the verified token. `KbEntry.requires_entitlement` gates on what the tenant has BOUGHT
(feature.X) — a fact that changes the instant a shop subscribes or cancels, is never minted into the
token, and must therefore be resolved FRESH at request time.

WHY A PORT, AND WHY THE CALLER MAY NOT SUPPLY IT. This mirrors KnowledgeSource's rule exactly: the
caller says what it is ASKING, never what is TRUE. If a client could post its own held set, any client
could reveal an unbought app's detailed usage KB by claiming the entitlement — the same class of hole
`requires` was built to close ("a customer app could read admin help simply by claiming to be admin").
So the held set is resolved from an authoritative source an operator configured, behind this port; the
caller only supplies the (token-verified) tenant it is acting for.

FAIL CLOSED. The null resolver returns the empty set, so with nothing wired an entitlement-gated entry
is invisible to everyone — a marketing-tier entry (no requires_entitlement) still shows; a detailed one
stays hidden until a real resolver confirms the tenant holds its feature key. Never the other way.
"""
from __future__ import annotations

from typing import Protocol, runtime_checkable


@runtime_checkable
class EntitlementResolver(Protocol):
    driver: str

    async def held(self, tenant: str) -> frozenset[str]:
        """The feature keys `tenant` currently holds (e.g. {"feature.loyalty"}). Empty when nothing is
        wired or the tenant holds nothing. MUST NOT raise for a resolution failure — return empty
        (fail-closed to invisible), never crash the ask."""
        ...
