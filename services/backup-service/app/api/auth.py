# =============================================================================
# backup-service — Authentication (real token verification)
# -----------------------------------------------------------------------------
# require_user verifies the Bearer token via the configured TokenVerifier port
# (deny-by-default: the 'null' driver rejects everything until real auth is set).
# On success a Principal is attached to request.state and returned.
# =============================================================================
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from fastapi import HTTPException, Request, status

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
        claims = verifier.verify(token)
    except InvalidToken:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="INVALID_TOKEN")
    principal = _extract_principal(claims)
    request.state.principal = principal
    return principal
