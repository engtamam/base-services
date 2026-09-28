"""The inert gate: every tenant reads as active. Default driver, so the enforcement primitive ships
non-breaking — a deployment opts IN by wiring the `tenant` driver and setting the enforcement mode."""
from __future__ import annotations

from app.ports.tenant_gate import ACTIVE


class OffTenantGate:
    driver = "off"

    async def status(self, tenant: str) -> str:
        return ACTIVE
