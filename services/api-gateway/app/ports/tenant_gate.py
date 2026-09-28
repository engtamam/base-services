"""TenantGate port — resolve a tenant's lifecycle status for access enforcement.

status(tenant) returns one of: "active", "suspended", "deleted", "unknown". The gateway blocks every
authenticated request for a tenant that is not active (see app/core/tenant_status). TenantGateUnavailable
means the status could not be resolved (transport error) — the caller applies its fail-open/closed policy.
"""
from __future__ import annotations

from typing import Protocol, runtime_checkable

ACTIVE = "active"
SUSPENDED = "suspended"
DELETED = "deleted"
UNKNOWN = "unknown"


class TenantInactive(Exception):
    def __init__(self, tenant: str, tenant_status: str) -> None:
        super().__init__("TENANT_INACTIVE")
        self.tenant = tenant
        self.tenant_status = tenant_status


class TenantGateUnavailable(Exception):
    pass


@runtime_checkable
class TenantGate(Protocol):
    driver: str

    async def status(self, tenant: str) -> str: ...
