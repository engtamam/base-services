"""The disabled tenant gate: every tenant reads as active.

Wired when IDENTITY_TENANT_STATUS_ENFORCEMENT=off (the default), so every mint path behaves exactly
as it did before status enforcement existed — no tenant-service dependency, no network call."""
from __future__ import annotations

from app.ports.tenant_gate import ACTIVE


class NoTenantGate:
    driver = "off"

    async def status(self, tenant: str) -> str:
        return ACTIVE
