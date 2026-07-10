# =============================================================================
# feature-flag-service — Request principal / tenant context
# =============================================================================
from __future__ import annotations

from fastapi import Depends, Request

from app.api.auth import Principal, require_user


def current_principal(request: Request) -> Principal | None:
    return getattr(request.state, "principal", None)


async def tenant_scope(principal: Principal = Depends(require_user)) -> str | None:
    """The verified caller's tenant id (or None), for datastore scoping."""
    return principal.tenant
