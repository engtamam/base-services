"""Multi-tenant membership routes — one login, many companies, one room at a time.

Mounted ONLY when IDENTITY_MULTI_TENANT is on. Off, these routes do not exist at all: a
single-tenant deployment gets 404, not a half-built feature it can be talked into using.

The flow, and why it is two steps:

    POST /auth/person/login    prove who you are      -> person + the doors you hold. NO DATA.
    POST /auth/tenant          choose ONE door        -> a token scoped to exactly that tenant.

Login deliberately mints no token. Until a tenant is chosen there is nothing a token could be
scoped to, and a tenant-less token is precisely the thing sixteen downstream services have no way
to check (T8). So we do not create one.
"""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Request, status
from pydantic import BaseModel, Field

from app.api.ratelimit import rate_limit
from app.api.v1.auth import _svc
from app.config import get_settings
from app.core.tenancy import TenantAccessDenied
from app.ports.errors import InvalidToken, ValidationError

router = APIRouter(prefix="/api/v1/auth", tags=["tenancy"])


class PersonLoginIn(BaseModel):
    username: str = Field(max_length=256)
    password: str = Field(min_length=1, max_length=256)
    code: str | None = None                      # MFA code when enrolled


class TenantIn(BaseModel):
    tenant: str = Field(min_length=1, max_length=128)
    # Credentials are OPTIONAL: an existing access token proves the person just as well, and that
    # is what makes switching tenants possible without retyping a password. They become REQUIRED
    # when IDENTITY_TENANT_SWITCH_REAUTH is on.
    username: str | None = None
    password: str | None = None
    code: str | None = None


def _source(request: Request) -> str:
    """The lockout bucket. Keyed with the caller so brute-forcing one person from one source
    cannot lock that person out of every client they have (T5)."""
    return request.client.host if request.client else ""


@router.post("/person/login", dependencies=[Depends(rate_limit)])
async def person_login(request: Request, body: PersonLoginIn) -> dict:
    """Authenticate the PERSON. Returns their doors — and no token, and no data."""
    svc = _svc(request)
    try:
        person = await svc.authenticate_person(body.username, body.password, _source(request))
    except ValidationError:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "INVALID_CREDENTIALS")
    if person is None:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "INVALID_CREDENTIALS")

    c = request.app.state.container
    if c.mfa.enabled and person.mfa_secret:
        if not body.code or not c.mfa.verify(person.mfa_secret, body.code):
            raise HTTPException(status.HTTP_401_UNAUTHORIZED, "MFA_REQUIRED")

    memberships = await svc.memberships.memberships(person.id)
    return {
        "person": person.public(),
        # Only the doors THIS person holds, and only the fields they may see about their own
        # membership. Never another person's, never another tenant's member list.
        "memberships": [m.public() for m in memberships if m.active],
    }


@router.post("/tenant", dependencies=[Depends(rate_limit)])
async def select_tenant(request: Request, body: TenantIn) -> dict:
    """Choose ONE door and get a token scoped to it.

    This is THE mint, and it is the only door between every company on the platform. The tenant
    that ends up in the token is read off the membership row we find — never off `body.tenant`
    (T1). `body.tenant` is a request to be let in, not an assertion of where you already are.
    """
    s = get_settings()
    svc = _svc(request)
    c = request.app.state.container

    person = None
    if body.username and body.password:
        try:
            person = await svc.authenticate_person(body.username, body.password, _source(request))
        except ValidationError:
            person = None
        if person is not None and c.mfa.enabled and person.mfa_secret:
            if not body.code or not c.mfa.verify(person.mfa_secret, body.code):
                raise HTTPException(status.HTTP_401_UNAUTHORIZED, "MFA_REQUIRED")
    elif not s.IDENTITY_TENANT_SWITCH_REAUTH:
        # Switching on an existing session: the access token proves the person. It says nothing
        # about the tenant being switched TO — that is decided below, by the membership.
        auth = request.headers.get("authorization", "")
        scheme, _, token = auth.partition(" ")
        if scheme.lower() != "bearer" or not token:
            raise HTTPException(status.HTTP_401_UNAUTHORIZED, "INVALID_CREDENTIALS")
        try:
            claims = c.token_verifier.verify(token)
        except InvalidToken:
            raise HTTPException(status.HTTP_401_UNAUTHORIZED, "INVALID_CREDENTIALS")
        if claims.get("type") != "access":
            raise HTTPException(status.HTTP_401_UNAUTHORIZED, "INVALID_CREDENTIALS")
        person = await svc.memberships.person(str(claims.get("sub", "")))
    else:
        # Step-up is ON: a stolen session may not widen itself into a higher-trust tenant by
        # merely asking. Re-prove the person (T10).
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "REAUTH_REQUIRED")

    if person is None:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "INVALID_CREDENTIALS")

    try:
        return await svc.select_tenant(person, body.tenant)
    except TenantAccessDenied:
        # ONE answer (T6). "No such tenant" and "you are not a member" are the same 403, because
        # telling them apart tells a competitor which of their rivals your consultant works for.
        raise HTTPException(status.HTTP_403_FORBIDDEN, "TENANT_ACCESS_DENIED")
