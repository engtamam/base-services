"""Null resolver — holds nothing (by design), so an entitlement-gated entry is invisible to everyone.

The default, so the service boots with zero infrastructure. Safe direction: a marketing-tier entry
(no requires_entitlement) still shows; a detailed, entitlement-gated one stays hidden until a real
resolver confirms the tenant holds its feature key. A null resolver that returned a non-empty set
would be the one dangerous mistake here — it would reveal bought-only KB to tenants that bought
nothing — so it returns the empty set, always.
"""
from __future__ import annotations


class NullEntitlements:
    driver = "null"

    def __init__(self) -> None:
        self.asked: list[str] = []

    async def held(self, tenant: str) -> frozenset[str]:
        # Recorded so a test can prove the resolver was REACHED, not skipped.
        self.asked.append(tenant)
        return frozenset()
