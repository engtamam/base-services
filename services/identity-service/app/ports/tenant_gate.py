"""TenantGate — is this tenant still allowed to exist on the platform, checked at MINT time.

The split, like LegalGate and CapsResolver:

    tenant-service owns  "acme was suspended / deleted"          — the lifecycle record.
    identity       owns  "so no token is minted for acme's users" — the enforcement.

Before this, tenant.status was enforced only by tenant-service's own self-service edit routes: a
suspended or deleted tenant's users could still log in and use every feature. Enforcement has to
happen at the MINT — the one moment a token is created — or a route-level check is one new route away
from being bypassed, and refresh would let a suspended tenant's session live for ever on renewals.

status(tenant) returns "active" | "suspended" | "deleted" | "unknown". Raises TenantGateUnavailable if
tenant-service cannot be reached — the caller decides fail-open vs fail-closed, because "active" here
MUST mean active, never "I could not ask".
"""
from __future__ import annotations

from typing import Protocol

ACTIVE = "active"
SUSPENDED = "suspended"
DELETED = "deleted"
UNKNOWN = "unknown"


class TenantGate(Protocol):
    driver: str

    async def status(self, tenant: str) -> str: ...


class TenantGateUnavailable(Exception):
    """tenant-service could not be reached. Like LegalGateUnavailable this does NOT dictate
    fail-closed: a status check that takes down all logins the instant tenant-service hiccups is its
    own outage, and a brief gap is no worse than the pre-enforcement world. The fail mode is an
    operator knob (IDENTITY_TENANT_GATE_FAIL_MODE)."""
