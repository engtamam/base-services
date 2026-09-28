# =============================================================================
# api-gateway — Authentication (real token verification)
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

from app.config import get_settings
from app.core.tenant_status import enforce_tenant_active
from app.ports.errors import InvalidToken
from app.ports.tenant_gate import TenantGateUnavailable, TenantInactive


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

    # A valid token is not enough: a SUSPENDED or DELETED tenant loses ALL access, not just its own
    # self-service edits. The status is resolved through the (cached) tenant gate so this is not a
    # synchronous cross-service call per request. Opt-in via config; the 'off' driver is a no-op.
    s = get_settings()
    gate = getattr(request.app.state.container, "tenant_gate", None)
    if gate is not None:
        try:
            await enforce_tenant_active(
                gate, principal.tenant,
                enforcement=s.TENANT_STATUS_ENFORCEMENT,
                platform_tenant=s.PLATFORM_TENANT,
                fail_mode=s.TENANT_STATUS_FAIL_MODE,
                block_unknown=s.TENANT_STATUS_BLOCK_UNKNOWN)
        except TenantInactive as exc:
            raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="TENANT_INACTIVE") from exc
        except TenantGateUnavailable as exc:
            raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                                detail="TENANT_STATUS_UNAVAILABLE") from exc
    return principal
