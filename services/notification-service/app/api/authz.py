# =============================================================================
# notification-service — Capability RBAC (deny-by-default)
# -----------------------------------------------------------------------------
# The `caps` claim is the SOLE authority (RBAC-v2, generalized). A token without
# caps resolves to NO capabilities — a `role` string alone can never grant
# access (defeats forged role-only escalation, CWE-285/CWE-269). 403 is generic
# and never leaks which capability was missing.
# =============================================================================
from __future__ import annotations

from fastapi import Depends, HTTPException, status

from app.api.auth import Principal, require_user


def effective_caps(principal: Principal) -> frozenset[str]:
    return principal.caps


def require_capability(*caps: str):
    """Require ALL of the given capabilities."""

    async def dep(principal: Principal = Depends(require_user)) -> Principal:
        held = effective_caps(principal)
        if not all(c in held for c in caps):
            raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="FORBIDDEN")
        return principal

    return dep


def require_any_capability(*caps: str):
    """Require AT LEAST ONE of the given capabilities."""

    async def dep(principal: Principal = Depends(require_user)) -> Principal:
        held = effective_caps(principal)
        if not any(c in held for c in caps):
            raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="FORBIDDEN")
        return principal

    return dep
