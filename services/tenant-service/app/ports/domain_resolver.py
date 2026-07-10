"""DomainResolver port — map an inbound host to a tenant slug."""
from __future__ import annotations

from typing import Protocol, runtime_checkable


@runtime_checkable
class DomainResolver(Protocol):
    driver: str

    async def tenant_slug_for(self, host: str) -> str | None:
        """Return the tenant slug for a host, or None if it can't be resolved."""
        ...
