# =============================================================================
# identity-service — Internal tier (service-to-service, X-Internal-Token)
# -----------------------------------------------------------------------------
# NOT user-facing. Guarded by a shared token compared in CONSTANT TIME
# (CWE-208: observable timing discrepancy). Never a user JWT.
# =============================================================================
from __future__ import annotations

import hmac

from fastapi import APIRouter, Depends, Header, HTTPException, Query, Request, status

from app.api.v1.tenancy import list_members_of
from app.config import Settings, get_settings
from app.core import facts
from app.core.membership import MembershipStore
from app.core.erasure import IdentityErasure
from app.core.refresh_store import RefreshStore
from app.core.user_repository import UserRepository
from app.manifest import build_manifest
from pydantic import BaseModel, Field

router = APIRouter(prefix="/internal", tags=["internal"])

_HEADER = "X-Internal-Token"


async def require_internal_token(
    x_internal_token: str | None = Header(default=None, alias=_HEADER),
    settings: Settings = Depends(get_settings),
) -> None:
    """Deny unless the caller presents the exact internal token (timing-safe)."""
    expected = settings.INTERNAL_SERVICE_TOKEN
    provided = x_internal_token or ""
    # hmac.compare_digest is constant-time and length-safe.
    if not hmac.compare_digest(provided, expected):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED, detail="UNAUTHORIZED"
        )


@router.get("/health", dependencies=[Depends(require_internal_token)])
async def internal_health(settings: Settings = Depends(get_settings)) -> dict[str, str]:
    return {"status": "ok", "service": settings.SERVICE_NAME}


@router.get("/manifest", dependencies=[Depends(require_internal_token)])
async def internal_manifest(
    request: Request, settings: Settings = Depends(get_settings)
) -> dict:
    container = getattr(request.app.state, "container", None)
    ports = container.describe() if container is not None else []
    manifest = build_manifest(settings, ports=ports)
    guard = getattr(request.app.state, "cwe_guard", None)
    if guard is not None:
        manifest["security"] = {"cwe_guard": guard.report()}
    return manifest


class EraseIn(BaseModel):
    tenant: str
    subject: str


@router.post("/privacy/erase", dependencies=[Depends(require_internal_token)])
async def erase_subject(request: Request, body: EraseIn) -> dict:
    """Erase a data subject. Called by compliance-service during a DSAR fan-out.

    identity owns the pseudonym LINKAGE, so this is the call that actually makes the person
    unlinkable everywhere else. Idempotent: a retry on an already-erased subject returns the same
    shape rather than failing, because a DSAR retries.
    """
    # Write-ahead: a critical action the log cannot take must not happen at all, so this
    # runs BEFORE the mutation. Recording afterwards would 503 a caller whose action had
    # already succeeded, and leave it unrecorded.
    await request.app.state.auditor.record(
        "subject.erased", actor="", tenant=body.tenant, resource=f"subject:{body.subject}",
        ip=request.client.host if request.client else "")
    c = request.app.state.container
    s = get_settings()
    eraser = IdentityErasure(
        users=UserRepository(c.datastore, c.login_identifier),
        refresh=RefreshStore(c.datastore),
        store=c.datastore,
        pii_salt=s.PII_SALT,
        # Without these the Person row — identifier, password hash, MFA secret — survives a DSAR
        # while the receipt claims nothing was kept.
        memberships=MembershipStore(c.datastore) if s.IDENTITY_MULTI_TENANT else None,
        identifier=c.login_identifier if s.IDENTITY_MULTI_TENANT else None,
    )
    return await eraser.erase(body.tenant, body.subject)


class UnblockIn(BaseModel):
    key: str = Field(min_length=1, max_length=256)


@router.post("/ratelimit/unblock", dependencies=[Depends(require_internal_token)])
async def ratelimit_unblock(request: Request, body: UnblockIn) -> dict:
    """Clear ONE rate-limit key. The operator's 3am remedy.

    Someone WILL be locked out by a misconfigured limit or a shared NAT. If the only tool is
    FLUSHALL, the operator uses it and takes every other limit on the platform down with it. This
    clears exactly one key and nothing else — the surgical alternative to the sledgehammer.

    The key is the composite key the policy built (e.g. `auth|s:<subject>`); an operator reads it
    off the throttled response's logs. It is internal-token gated: unblocking a limit is a platform
    action, not a tenant one.
    """
    # Write-ahead: a critical action the log cannot take must not happen at all, so this
    # runs BEFORE the mutation. Recording afterwards would 503 a caller whose action had
    # already succeeded, and leave it unrecorded.
    await request.app.state.auditor.record(
        "ratelimit.unblocked", actor="", tenant="", resource="ratelimit",
        ip=request.client.host if request.client else "")
    await request.app.state.container.rate_limiter.reset(body.key)
    return {"unblocked": True, "key": body.key}


@router.get("/identity/facts/{fact}", dependencies=[Depends(require_internal_token)])
async def identity_fact(request: Request, fact: str, tenant: str, subject: str) -> dict:
    """Answer a question another service declared identity as the OWNER of.

    onboarding names requirements `satisfied_by: "identity.email_verified"`, which only works if
    identity can be asked. It could not be, so those flows could never complete.

    THREE OUTCOMES, and keeping them apart is the entire safety property:

      200 {"satisfied": true|false}  we know, and this is the answer
      404                            no such account — the caller reads this as UNSATISFIED, which
                                     is right: a person who does not exist has verified nothing
      501                            WE DO NOT ANSWER THAT QUESTION. The caller reads any non-404
                                     error as UNKNOWN, and unknown never counts as satisfied.

    The third is why an unrecognised fact must not be a 404. A 404 would be read as a decision —
    "this requirement is not met" — when the truth is that nobody evaluated it. For a requirement
    that gates a welcome credit, the difference is whether a stranger is refused or quietly
    onboarded.
    """
    s = get_settings()
    c = request.app.state.container
    try:
        answer = await facts.resolve(
            fact,
            users=UserRepository(c.datastore, c.login_identifier),
            memberships=MembershipStore(c.datastore) if s.IDENTITY_MULTI_TENANT else None,
            tenant=tenant, subject=subject, legacy=s.EMAIL_VERIFICATION_LEGACY)
    except facts.UnknownFact:
        raise HTTPException(status.HTTP_501_NOT_IMPLEMENTED, "UNKNOWN_FACT")
    if answer is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "NOT_FOUND")
    return {"fact": fact, "satisfied": bool(answer)}


@router.get("/tenancy/members", dependencies=[Depends(require_internal_token)])
async def internal_members(request: Request, tenant: str = Query(..., min_length=1),
                           limit: int | None = Query(default=None, ge=1),
                           after: str | None = Query(default=None)) -> dict:
    """The member directory for a background JOB — no user JWT required.

    The user-facing read gates on a `members:read` CAPABILITY and takes the tenant off the caller's
    token, which is exactly right for a person clicking a screen and wrong for a service. A document
    renderer, an export worker, a billing sweep — these hold the INTERNAL token, not a user JWT with
    caps, so they had no way to enumerate a tenant's members at all.

    Here the tenant IS a parameter, and that is safe precisely because the caller is internal: it
    holds the platform's service token and is trusted to name the tenant it is working for. It is
    REQUIRED (min_length=1) — there is deliberately no "all tenants" value, so a bug that forgets to
    set it fails loudly rather than dumping every company's staff. The projection is the same one the
    admin route uses: identifier, roles, caps, status — never a credential.
    """
    if not get_settings().IDENTITY_MULTI_TENANT:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "MULTI_TENANT_DISABLED")
    s = get_settings()
    page = min(limit or s.IDENTITY_MEMBERS_PAGE_SIZE, s.IDENTITY_MEMBERS_MAX_PAGE_SIZE)
    store = MembershipStore(request.app.state.container.datastore)
    return await list_members_of(store, tenant, page, after)


class SeedOwnerIn(BaseModel):
    tenant: str = Field(min_length=1, max_length=128)
    identifier: str = Field(min_length=1, max_length=320)
    roles: list[str] = Field(default_factory=lambda: ["owner"], max_length=32)
    # Empty means "the configured members-write cap" — the route fills it in. A caller may pass more.
    caps: list[str] = Field(default_factory=list, max_length=64)
    password: str | None = Field(default=None, max_length=1024)
    only_if_empty: bool = True


@router.post("/tenancy/seed-owner", status_code=status.HTTP_201_CREATED,
             dependencies=[Depends(require_internal_token)])
async def seed_owner(request: Request, body: SeedOwnerIn) -> dict:
    """Bootstrap a tenant's FIRST owner as a Person, over the internal token.

    The chicken-and-egg breaker: a freshly provisioned tenant has nobody holding members:write, so
    no USER route can add the first admin. This service-to-service door does — guarded so it stays a
    bootstrap primitive, not a grant backdoor: the owner must hold the members-write cap, and
    `only_if_empty` refuses a DIFFERENT owner on an already-staffed tenant. Idempotent; reuses an
    existing Person without touching their credential (T2).

    Audited WRITE-AHEAD and CRITICAL: an owner grant that cannot be recorded must not happen — an
    unattributable owner is exactly what an attacker wants. The seeded human is recorded as a
    PSEUDONYM, never the raw identifier, so the trail is provable (resolve it here) yet erasable.
    """
    from app.api.v1.auth import _svc
    from app.core.auth_service import CredentialNotYours, SeedError, WeakPassword
    from app.core.pii import subject_ref
    from app.core.tenancy import TenantAccessDenied

    s = get_settings()
    if not s.IDENTITY_MULTI_TENANT:
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, "MULTI_TENANT_OFF")
    write_cap = s.IDENTITY_MEMBERS_WRITE_CAP
    caps = list(body.caps) or [write_cap]
    owner_ref = subject_ref(s.PII_SALT, body.tenant, body.identifier) if s.PII_SALT else ""

    async def _audit_write_ahead() -> None:
        # CRITICAL (declared in auditable_actions): if this raises AuditUnavailable, seed_owner
        # aborts before any grant. Records the tenant + a PSEUDONYM of the seeded human + the roles —
        # never the raw identifier and never the credential.
        await request.app.state.auditor.record(
            "tenant.owner_seeded", actor="", tenant=body.tenant, resource=f"tenant:{body.tenant}",
            ip=request.client.host if request.client else "",
            metadata={"owner_ref": owner_ref, "roles": ",".join(body.roles)})

    try:
        res = await _svc(request).seed_owner(
            body.tenant, body.identifier, roles=body.roles, caps=caps, password=body.password,
            invite_ttl=s.IDENTITY_INVITE_TTL_SECONDS, only_if_empty=body.only_if_empty,
            write_cap=write_cap, on_commit=_audit_write_ahead)
        # `id` is the undo reference an orchestrator (onboarding saga) captures to compensate: if a
        # later provisioning step fails, it DELETEs this membership so a failed signup leaves no
        # orphan owner. tenant::person_id — neither contains "::".
        res["id"] = f"{res['tenant']}::{res['person_id']}"
        return res
    except SeedError as exc:
        raise HTTPException(status.HTTP_409_CONFLICT, str(exc)) from exc
    except CredentialNotYours:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "CANNOT_SET_EXISTING_PERSON_CREDENTIAL")
    except WeakPassword:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "WEAK_PASSWORD")
    except TenantAccessDenied:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "TENANT_ACCESS_DENIED")


@router.delete("/tenancy/membership/{ref}", dependencies=[Depends(require_internal_token)])
async def revoke_membership(request: Request, ref: str) -> dict:
    """Compensation for a seeded owner: revoke ONE membership, idempotently.

    `ref` is the `id` seed-owner returned (tenant::person_id). This is the undo the onboarding saga
    calls when a later provisioning step fails, so a failed signup does not leave an orphan owner. It
    revokes the membership only — never the Person (they may work elsewhere, T2). Idempotent:
    revoking an already-gone membership is success, which is what a resumed rollback re-attempts.
    """
    if not get_settings().IDENTITY_MULTI_TENANT:
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, "MULTI_TENANT_OFF")
    tenant, sep, person_id = ref.partition("::")
    if not sep or not tenant or not person_id:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "BAD_REFERENCE")
    store = MembershipStore(request.app.state.container.datastore)
    existed = await store.membership(person_id, tenant) is not None
    if existed:
        # member.removed is CRITICAL and write-ahead: removing access must be provable.
        await request.app.state.auditor.record(
            "member.removed", actor="", tenant=tenant, resource=f"member:{person_id}",
            ip=request.client.host if request.client else "")
        await store.revoke(person_id, tenant)
    return {"revoked": existed, "tenant": tenant, "person_id": person_id}
