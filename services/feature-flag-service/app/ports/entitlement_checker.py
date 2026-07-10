"""EntitlementChecker port — a flag may REQUIRE a plan entitlement.

Flags answer "is this code path on"; entitlements answer "does this plan allow it".
billing-service stays the source of truth; `null` allows everything (dev / non-billed).
"""
from __future__ import annotations

from typing import Protocol, runtime_checkable


@runtime_checkable
class EntitlementChecker(Protocol):
    driver: str

    def allowed(self, *, tenant: str, key: str) -> bool:
        """True if the tenant's plan entitles `key`. Never raises — fail-closed on error."""
        ...
