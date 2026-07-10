"""IsolationStrategy port — resolve a tenant's storage scope.

tenant-service is the source of truth for HOW a tenant is isolated; peers use the
returned scope to scope their own storage (shared-row filter / schema / db).
"""
from __future__ import annotations

from typing import Protocol, runtime_checkable


@runtime_checkable
class IsolationStrategy(Protocol):
    driver: str

    def scope(self, tenant_id: str, slug: str) -> dict[str, str]:
        """Return {mode, namespace|schema|dsn} for the tenant."""
        ...
