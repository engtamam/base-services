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

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from pydantic import BaseModel, Field

from app.api.auth import Principal, require_cap
from app.api.ratelimit import rate_limit
from app.api.v1.auth import _svc
from app.config import get_settings
from app.core.auth_service import CredentialNotYours, SoleOwnerError, WeakPassword
from app.ports.caps_resolver import CapsUnavailable
from app.core.tenancy import NoTenantClaim, TenantAccessDenied, tenant_of
from app.ports.errors import InvalidToken, ValidationError

router = APIRouter(prefix="/api/v1/auth", tags=["tenancy"])

# The directory lives under its own prefix: it is not an auth flow, it is a read of who works here.
members_router = APIRouter(prefix="/api/v1/tenancy", tags=["tenancy"])


def _members_cap():
    """Resolved per request, so the capability name is config and not import-time furniture."""
    return require_cap(get_settings().IDENTITY_MEMBERS_READ_CAP)


def _write_cap():
    return require_cap(get_settings().IDENTITY_MEMBERS_WRITE_CAP)


async def _caller_tenant(request: Request, dep) -> tuple[Principal, str]:
    """The caller, and the tenant they are ACTUALLY in — off the token, never off the request."""
    principal: Principal = await dep(request)
    try:
        return principal, tenant_of(principal.raw)
    except NoTenantClaim:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "NO_TENANT")


class AddMemberIn(BaseModel):
    identifier: str = Field(min_length=1, max_length=256)
    roles: list[str] = Field(default_factory=list)
    caps: list[str] = Field(default_factory=list)
    # Only ever honoured in `password` mode for a person who does not exist yet. For anyone who
    # already exists it is REFUSED, not ignored — see AuthService.add_member (T2).
    password: str | None = Field(default=None, max_length=256)


class ClaimIn(BaseModel):
    token: str = Field(min_length=1, max_length=512)
    password: str = Field(min_length=1, max_length=256)  # core enforces the minimum length


@members_router.post("/members", status_code=status.HTTP_201_CREATED,
                     dependencies=[Depends(rate_limit)])
async def add_member(request: Request, body: AddMemberIn) -> dict:
    """Staff THIS tenant. Admin-gated; the tenant comes off the token.

    If the person already exists we grant a membership and touch nothing else — their credential
    is the key to every other company they work for, and an admin here has no claim on it (T2).
    """
    # AUTHORIZE FIRST, then write-ahead audit, then mutate.
    #
    # The record used to run before this line, which had two faults. It could not name the actor,
    # because the principal is only resolved here — and it recorded an ATTEMPT by a caller who
    # was about to be refused, so any unauthenticated client could append entries to the audit
    # chain at will. Write-ahead means before the MUTATION, not before the authorization.
    s = get_settings()
    principal, tenant = await _caller_tenant(request, _write_cap())
    # Write-ahead: a critical action the log cannot take must not happen at all, so this
    # runs BEFORE the mutation. Recording afterwards would 503 a caller whose action had
    # already succeeded, and leave it unrecorded.
    await request.app.state.auditor.record(
        "member.added", actor=principal.subject, tenant=tenant, resource="member",
        ip=request.client.host if request.client else "")
    svc = _svc(request)
    try:
        m, invite = await svc.add_member(
            tenant, body.identifier, roles=body.roles, caps=body.caps, password=body.password,
            mode=s.IDENTITY_MEMBER_ADD_MODE, invite_ttl=s.IDENTITY_INVITE_TTL_SECONDS)
    except ValidationError:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "INVALID_IDENTIFIER")
    except WeakPassword:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "WEAK_PASSWORD")
    except CredentialNotYours:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "CANNOT_SET_EXISTING_PERSON_CREDENTIAL")
    except TenantAccessDenied:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "TENANT_ACCESS_DENIED")

    out = {"member": m.public() | {"person_id": m.person_id, "caps": m.caps}}
    if invite:
        # Returned EXACTLY once and never stored in the clear. If the admin loses it, the invite is
        # reissued — it is not recoverable, by design.
        out["invite_token"] = invite
    return out


@members_router.delete("/members/{person_id}", dependencies=[Depends(rate_limit)])
async def remove_member(request: Request, person_id: str) -> dict:
    """Revoke ONE membership. The person survives — she still works somewhere else.

    Deleting the Person from a single company's admin panel would remove her from every other
    company on the platform. This revokes a key, not a human.
    """
    # Authorize first — see add_member above: recording before the capability check let an
    # unauthorized caller append to the audit chain and left the entry with no actor.
    principal, tenant = await _caller_tenant(request, _write_cap())
    # Write-ahead: a critical action the log cannot take must not happen at all, so this
    # runs BEFORE the mutation. Recording afterwards would 503 a caller whose action had
    # already succeeded, and leave it unrecorded.
    await request.app.state.auditor.record(
        "member.removed", actor=principal.subject, tenant=tenant,
        resource=f"member:{person_id}",
        ip=request.client.host if request.client else "")
    try:
        removed = await _svc(request).remove_member(tenant, person_id)
    except SoleOwnerError:
        # Removing the tenant's last owner would strand it — the same invariant identity.self
        # enforces for a sole owner deleting themselves.
        raise HTTPException(status.HTTP_409_CONFLICT, "LAST_OWNER")
    if not removed:
        # Same answer whether they are in another tenant or do not exist at all: a 404 that
        # distinguished the two would let an admin probe for who works elsewhere (T6).
        raise HTTPException(status.HTTP_404_NOT_FOUND, "NOT_A_MEMBER")
    return {"removed": True, "person_id": person_id, "tenant": tenant}


class UpdateMemberIn(BaseModel):
    roles: list[str] | None = None
    caps: list[str] | None = None


@members_router.put("/members/{person_id}", dependencies=[Depends(rate_limit)])
async def update_member(request: Request, person_id: str, body: UpdateMemberIn) -> dict:
    """Change a member's roles/caps in THIS tenant — never their credential (T2).

    Without this, changing what a member may do meant removing and re-inviting them: it dropped the
    membership and forced a fresh password claim. This edits only the roles/caps on the membership.
    A role change is a change in who holds power, so it is audited write-ahead as critical."""
    principal, tenant = await _caller_tenant(request, _write_cap())
    await request.app.state.auditor.record(
        "member.roles_changed", actor=principal.subject, tenant=tenant,
        resource=f"member:{person_id}", ip=request.client.host if request.client else "",
        metadata={"roles": ",".join(body.roles or []), "caps": ",".join(body.caps or [])})
    try:
        m = await _svc(request).update_member_roles(
            tenant, person_id, roles=body.roles, caps=body.caps)
    except SoleOwnerError:
        # Stripping `owner` from the tenant's last owner strands it.
        raise HTTPException(status.HTTP_409_CONFLICT, "LAST_OWNER")
    if m is None:
        # Same 404 as remove: never distinguish "in another tenant" from "does not exist" (T6).
        raise HTTPException(status.HTTP_404_NOT_FOUND, "NOT_A_MEMBER")
    return {"member": m.public() | {"person_id": m.person_id, "caps": m.caps}}


class MemberStatusIn(BaseModel):
    status: str = Field(pattern="^(active|suspended)$")


@members_router.post("/members/{person_id}/status", dependencies=[Depends(rate_limit)])
async def set_member_status(request: Request, person_id: str, body: MemberStatusIn) -> dict:
    """Suspend or reactivate a member in THIS tenant, without deleting them.

    Suspension revokes ACCESS while keeping the person, their roles and their caps: the same seam
    that enforces removal (`authorize_tenant` refuses a non-active membership) ends a suspended
    member's access on their next refresh, within the access-token TTL. Reactivation restores it.
    Tenant comes from the token, never the body. Idempotent, and audited write-ahead as critical —
    a change in whether someone can act is disputable exactly like a role change."""
    principal, tenant = await _caller_tenant(request, _write_cap())
    await request.app.state.auditor.record(
        "member.status_changed", actor=principal.subject, tenant=tenant,
        resource=f"member:{person_id}", ip=request.client.host if request.client else "",
        metadata={"status": body.status})
    try:
        m = await _svc(request).set_member_status(tenant, person_id, body.status)
    except SoleOwnerError:
        # Suspending the tenant's last owner revokes the only access that can manage it.
        raise HTTPException(status.HTTP_409_CONFLICT, "LAST_OWNER")
    if m is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "NOT_A_MEMBER")
    return {"member": m.public() | {"person_id": m.person_id, "caps": m.caps}}


@router.post("/person/claim", dependencies=[Depends(rate_limit)])
async def claim_invite(request: Request, body: ClaimIn) -> dict:
    """The invited human sets their OWN password. No token, no admin, no session — just the invite.

    Single-use: redeeming consumes it. An invite that worked twice would be a password reset for
    anyone who ever saw the link.
    """
    svc = _svc(request)
    try:
        person = await svc.claim_invite(body.token, body.password)
    except WeakPassword:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "WEAK_PASSWORD")
    if person is None:
        # Unknown, expired, already used, or for a person who has since claimed: one answer.
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "INVALID_INVITE")
    return {"person": person.public()}


@members_router.get("/members", dependencies=[Depends(rate_limit)])
async def list_members(
    request: Request,
    limit: int | None = Query(default=None, ge=1),
    after: str | None = Query(default=None),
) -> dict:
    """Who works HERE. Admin-gated, one tenant, paginated.

    THE TENANT IS NOT A PARAMETER. It is read off the caller's token — the same membership row the
    mint verified. A `?tenant=` on a directory read is T1 wearing a different hat: it would let any
    admin enumerate the staff of any company on the platform, and it would look like a feature.

    A caller with no tenant claim is REJECTED, never widened to "all tenants" (T8).
    """
    s = get_settings()
    principal: Principal = await _members_cap()(request)
    try:
        tenant = tenant_of(principal.raw)
    except NoTenantClaim:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "NO_TENANT")

    page = min(limit or s.IDENTITY_MEMBERS_PAGE_SIZE, s.IDENTITY_MEMBERS_MAX_PAGE_SIZE)
    return await list_members_of(_svc(request).memberships, tenant, page, after)


async def list_members_of(store, tenant: str, page: int, after: str | None) -> dict:
    """Build the member directory for ONE tenant. Shared by the user-facing route (tenant off the
    token) and the internal route (tenant an explicit parameter). The projection is identical, so a
    background job and an admin see exactly the same fields — never a password hash or an MFA secret,
    which have no business in a directory read whoever is asking."""
    rows, cursor = await store.members(tenant, limit=page, after=after)
    members = []
    for m in rows:
        person = await store.person(m.person_id)
        members.append({
            "person_id": m.person_id,
            "identifier": person.identifier if person else None,   # a gone person -> absent, not skipped
            "roles": m.roles,
            "caps": m.caps,
            "status": m.status,
            "since": m.created_at,
        })
    return {"tenant": tenant, "members": members, "next": cursor}


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
    except CapsUnavailable:
        # The capability authority is down. We refuse the login rather than mint a token with no
        # capabilities: a zero-cap token looks exactly like a permissions bug, and support "fixes"
        # those by granting caps by hand — so an outage would quietly become a permanent
        # over-grant. 503 says "come back", which is true and reversible.
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, "CAPS_UNAVAILABLE")
    except TenantAccessDenied:
        # ONE answer (T6). "No such tenant" and "you are not a member" are the same 403, because
        # telling them apart tells a competitor which of their rivals your consultant works for.
        raise HTTPException(status.HTTP_403_FORBIDDEN, "TENANT_ACCESS_DENIED")
