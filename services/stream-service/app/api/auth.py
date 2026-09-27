# =============================================================================
# stream-service — Authentication (real token verification)
# -----------------------------------------------------------------------------
# require_user verifies the Bearer token via the configured TokenVerifier port
# (deny-by-default: the 'null' driver rejects everything until real auth is set).
# On success a Principal is attached to request.state and returned.
# =============================================================================
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from fastapi import HTTPException, Request, status
from starlette.concurrency import run_in_threadpool

from app.ports.errors import InvalidToken


@dataclass(frozen=True)
class Principal:
    subject: str
    caps: frozenset[str] = field(default_factory=frozenset)
    tenant: str | None = None
    raw: dict[str, Any] = field(default_factory=dict)


def _extract_principal(claims: dict[str, Any]) -> Principal:
    caps_claim = claims.get("caps")
    caps = frozenset(caps_claim) if isinstance(caps_claim, (list, tuple, set)) else frozenset()
    tenant = claims.get("tenant") or claims.get("org") or claims.get("tid")
    return Principal(
        subject=str(claims.get("sub", "")),
        caps=caps,
        tenant=str(tenant) if tenant else None,
        raw=claims,
    )


async def require_user(request: Request) -> Principal:
    auth = request.headers.get("authorization", "")
    if not auth.startswith("Bearer "):
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="NO_TOKEN")
    token = auth[7:]
    verifier = request.app.state.container.token_verifier
    try:
        # OFFLOAD: verify() is synchronous and blocks the single-worker event loop — on
        # jwks/rs256 it does a blocking JWKS fetch + CPU-bound RSA verify. See _template.
        claims = await run_in_threadpool(verifier.verify, token)
    except InvalidToken:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="INVALID_TOKEN")
    principal = _extract_principal(claims)
    request.state.principal = principal
    return principal


async def require_tenant(request: Request) -> Principal:
    """Tenant-scoped variant of require_user (audit stream-002).

    Every /api/v1 stream route is scoped to one tenant and historically fell back to
    `p.tenant or ""`, collapsing every principal whose token lacks a tenant/org/tid claim (or
    carries an empty/falsy one) into a single shared "" bucket — where two different customers could
    read and delete each other's sources, sessions, recordings and exports. The tenant is the only
    wall between companies, so a missing one is a hard refusal here, never a silent default onto a
    real shared bucket. (Operator routes — service secrets/config — are legitimately tenant-less and
    keep using require_user directly.)
    """
    principal = await require_user(request)
    if not principal.tenant:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="TENANT_REQUIRED")
    return principal
