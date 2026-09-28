"""The tenant-status enforcement decision, isolated from transport so it is unit-testable.

Policy (owner-approved): a SUSPENDED or DELETED tenant loses ALL access — every authenticated request
is refused, not just the tenant's own self-service edits. The platform tenant is never gated (it is the
operator plane, not a customer). An unresolvable status fails open by default (availability: a
tenant-service blip must not 401 the whole platform) with a loud WARN, or fails closed when configured.
A definite negative status is never softened by the fail mode — only a transport failure is.
"""
from __future__ import annotations

import logging

from app.ports.tenant_gate import (
    DELETED,
    SUSPENDED,
    UNKNOWN,
    TenantGateUnavailable,
    TenantInactive,
)

_log = logging.getLogger("api-gateway.tenant_status")


async def enforce_tenant_active(gate, tenant: str | None, *, enforcement: str, platform_tenant: str,
                                fail_mode: str, block_unknown: bool) -> None:
    """Raise TenantInactive if `tenant` is suspended/deleted (or unknown, when block_unknown). Raises
    TenantGateUnavailable only in fail-closed mode when the status cannot be resolved."""
    if enforcement != "block":
        return
    if not tenant or tenant == platform_tenant:
        return
    try:
        st = await gate.status(tenant)
    except TenantGateUnavailable as exc:
        if fail_mode == "closed":
            raise
        _log.warning("tenant_gate_unavailable_fail_open",
                     extra={"tenant": tenant, "detail": exc.__class__.__name__})
        return
    if st in (SUSPENDED, DELETED):
        raise TenantInactive(tenant, st)
    if st == UNKNOWN and block_unknown:
        raise TenantInactive(tenant, st)
