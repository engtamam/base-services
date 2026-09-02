# =============================================================================
# identity-service — Internal tier (service-to-service, X-Internal-Token)
# -----------------------------------------------------------------------------
# NOT user-facing. Guarded by a shared token compared in CONSTANT TIME
# (CWE-208: observable timing discrepancy). Never a user JWT.
# =============================================================================
from __future__ import annotations


from fastapi import APIRouter, Depends, Header, HTTPException, Query, Request, status

from app.api.v1.tenancy import list_members_of
from app.backup_manifest import BACKUP
from app.config import Settings, get_settings
from app.core import backup as _backup
from app.core import facts
from app.core.membership import MembershipStore
from app.core.erasure import IdentityErasure
from app.core.refresh_store import RefreshStore
from app.core.user_repository import UserRepository
from app.manifest import build_manifest
from pydantic import BaseModel, Field

router = APIRouter(prefix="/internal", tags=["internal"])

_HEADER = "X-Internal-Token"
_ASSERTION_HEADER = "X-Internal-Assertion"


async def require_internal_token(
    request: Request,
    x_internal_token: str | None = Header(default=None, alias=_HEADER),
    x_internal_assertion: str | None = Header(default=None, alias=_ASSERTION_HEADER),
    settings: Settings = Depends(get_settings),
) -> None:
    """Authenticate the internal caller, then authorise it for THIS route. The name is unchanged so
    every existing `Depends(require_internal_token)` keeps working, but it now does two things:

      1. IDENTITY — a config-resolved (cached) driver verifies the caller. `shared` (default) is
         the timing-safe flat-token compare (today's behaviour, unnamed peer); `static`/`jwks` verify a
         per-caller RS256 assertion. Any failure is one generic 401 — never which check failed.
      2. POLICY — the per-route allow-list decides whether that caller may reach `request.url.path`. A
         route with no policy admits any authenticated caller (default-open), so identity alone never
         breaks a path; a named allow-list yields 403 for a caller not on it.

    The verifier + policy are resolved from config (cached), not the container, so this seam is
    identical in every service."""
    from app.core.internal_caller_factory import resolve_internal_caller, resolve_internal_policy
    from app.ports.internal_caller import InternalCallerError

    audience = settings.INTERNAL_CALLER_AUDIENCE or settings.SERVICE_NAME
    try:
        identity = resolve_internal_caller(settings).verify(
            token=x_internal_token or "", assertion=x_internal_assertion or "", audience=audience)
    except InternalCallerError:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "UNAUTHORIZED")
    if not resolve_internal_policy(settings).permits(request.url.path, identity.caller):
        raise HTTPException(status.HTTP_403_FORBIDDEN, "FORBIDDEN")


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


class RotateSigningKeyResult(BaseModel):
    rotated: bool
    kid: str
    reason: str
    key_count: int


@router.post("/signing-key/rotate", dependencies=[Depends(require_internal_token)])
async def rotate_signing_key(
    request: Request, force: bool = Query(default=False)
) -> RotateSigningKeyResult:
    """Rotate the token SIGNING key (cron-driven; `force=true` for an emergency compromise rotation).

    Idempotent by design: a rotation is a no-op when the current key is younger than the configured
    min-age, so the k3s CronJob may fire repeatedly (or retry) without minting a pile of keys. The
    old public key stays in the JWKS for the overlap window, so no live token is rejected. Internal
    only — the cron authenticates with the shared internal token, never a user JWT.
    """
    sks = getattr(request.app.state, "signing_key_service", None)
    if sks is None:
        # Rotation is not enabled for this deployment (no keyring service was wired). A 503 tells
        # the cron this is a configuration state, not a transient failure to retry against.
        raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                            detail="SIGNING_KEY_ROTATION_DISABLED")
    result = await sks.rotate(force=force)
    if result.get("rotated"):
        auditor = getattr(request.app.state, "auditor", None)
        if auditor is not None:
            await auditor.record("identity.rotate_signing_key", actor="system", tenant="",
                                 metadata={"kid": result["kid"], "reason": result["reason"],
                                           "key_count": result["key_count"]})
    return RotateSigningKeyResult(**result)


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
        if res["seeded"]:
            # Emit the fact so consumers can light up (notification welcome, billing trial-start).
            # BEST-EFFORT and at-most-once: the durable record is the membership row and the audit,
            # never this event — a bus that is down must not fail a seed that already committed. No
            # raw identifier on the bus; person_id is the link a consumer resolves through identity.
            from app.ports.event_bus import Event
            try:
                await request.app.state.container.event_bus.publish(
                    "tenant.owner_seeded",
                    Event(type="tenant.owner_seeded",
                          payload={"tenant": res["tenant"], "person_id": res["person_id"],
                                   "created_person": res["created_person"], "roles": res["roles"]}))
            except Exception:  # noqa: BLE001 — a hint that fails to publish is not a failed seed
                pass
        return res
    except SeedError as exc:
        raise HTTPException(status.HTTP_409_CONFLICT, str(exc)) from exc
    except CredentialNotYours:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "CANNOT_SET_EXISTING_PERSON_CREDENTIAL")
    except WeakPassword:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "WEAK_PASSWORD")
    except TenantAccessDenied:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "TENANT_ACCESS_DENIED")


class InternalMemberIn(BaseModel):
    """Add an ORDINARY member service-to-service. ROLES ONLY — the product decides who is hired and
    with which role; capabilities are DERIVED from roles, so there is nothing to over-grant and no
    tenant admin ever needs members:write. Same shape as seed-owner minus only_if_empty (this is the
    second employee, not the first owner)."""
    tenant: str = Field(min_length=1, max_length=128)
    identifier: str = Field(min_length=1, max_length=320)
    roles: list[str] = Field(default_factory=list, max_length=32)
    password: str | None = Field(default=None, max_length=1024)


@router.post("/tenancy/members", status_code=status.HTTP_201_CREATED,
             dependencies=[Depends(require_internal_token)])
async def add_member_internal(request: Request, body: InternalMemberIn) -> dict:
    """The internal twin of POST /api/v1/tenancy/members — staff a tenant's SECOND (and Nth) member
    over the internal token.

    WHY IT EXISTS: seed-owner refuses to overwrite an existing owner (only_if_empty), so it is the
    first-account primitive and unusable for the second. Without this, a product could only staff a
    tenant by handing the shop owner members:write and letting the browser call identity — which is
    the privilege-escalation surface (direct caps) AND puts a platform-shaped API in front of a shop
    owner. This door hires everyone after the owner: ROLES ONLY (no caps parameter — capabilities are
    derived from roles), so it can never over-grant, and it is the same division that makes seed-owner
    safe applied to the Nth employee. Audited WRITE-AHEAD and CRITICAL; the human is a PSEUDONYM."""
    from app.api.v1.auth import _svc
    from app.core.auth_service import CredentialNotYours, WeakPassword
    from app.core.pii import subject_ref
    from app.core.tenancy import TenantAccessDenied
    from app.ports.errors import ValidationError

    s = get_settings()
    if not s.IDENTITY_MULTI_TENANT:
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, "MULTI_TENANT_OFF")
    member_ref = subject_ref(s.PII_SALT, body.tenant, body.identifier) if s.PII_SALT else ""
    # Write-ahead: an add that cannot be recorded must not happen. Records tenant + a PSEUDONYM of the
    # hired human + roles — never the raw identifier, never the credential.
    await request.app.state.auditor.record(
        "member.added", actor="", tenant=body.tenant, resource="member",
        ip=request.client.host if request.client else "",
        metadata={"member_ref": member_ref, "roles": ",".join(body.roles)})
    try:
        # granter_caps omitted (None) → the internal-bootstrap exemption; and caps=[] regardless, so
        # this route carries NO capability authority a role does not derive.
        m, invite = await _svc(request).add_member(
            body.tenant, body.identifier, roles=body.roles, caps=[], password=body.password,
            mode=s.IDENTITY_MEMBER_ADD_MODE, invite_ttl=s.IDENTITY_INVITE_TTL_SECONDS)
    except ValidationError:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "INVALID_IDENTIFIER")
    except WeakPassword:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "WEAK_PASSWORD")
    except CredentialNotYours:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "CANNOT_SET_EXISTING_PERSON_CREDENTIAL")
    except TenantAccessDenied:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "TENANT_ACCESS_DENIED")
    out = {"person_id": m.person_id, "tenant": m.tenant,   # the VERIFIED membership row, not the request
           "membership": m.public() | {"person_id": m.person_id, "roles": list(m.roles)}}
    if invite:
        # Returned EXACTLY once (invite mode), never stored in the clear — a lost invite is reissued.
        out["invite_token"] = invite
    return out


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


# ── Backup (backup-service orchestrates; this service produces sealed archives) ──────────────
# What identity-service holds is declared in app/backup_manifest.py; how it is read lives in
# core/backup.py. backup-service never opens this database — it calls export/verify/import over the
# internal token and stores the sealed archive, never seeing the plaintext.
def _now_iso() -> str:
    import datetime as _dt
    return _dt.datetime.now(_dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _sealer(settings: Settings):
    """The backup (seal, unseal) pair, or a 503 when no backup key is configured.

    Refused rather than defaulted: sealing with an empty key would write a plaintext-equivalent
    archive while every check reported it encrypted — the one outcome a backup must never have.
    """
    if not settings.BACKUP_KEK:
        raise HTTPException(
            status.HTTP_503_SERVICE_UNAVAILABLE,
            "BACKUP_KEK not configured — set this SHARED key (the SAME value backup-service holds) "
            "on this service, or its archives cannot be opened")
    return _backup.make_sealer(settings.BACKUP_KEK)


class BackupExportIn(BaseModel):
    # Empty tenant = the whole service; a value = just that customer's tenant-scoped rows.
    tenant: str = ""


class BackupVerifyIn(BaseModel):
    archive: dict


class BackupImportIn(BaseModel):
    archive: dict
    # Off by default: a restore must not bulldoze rows written after the backup unless the operator
    # driving the restore says so.
    overwrite: bool = False
    # Empty = restore in place. A value REMAPS the restore into that tenant (restore-as-clone).
    into_tenant: str = ""


@router.post("/backup/export", dependencies=[Depends(require_internal_token)])
async def backup_export(request: Request, body: BackupExportIn,
                        settings: Settings = Depends(get_settings)) -> dict:
    """Produce a sealed, checksummed archive of the accounts, memberships, and durable secrets."""
    seal, _ = _sealer(settings)
    return await _backup.export(
        request.app.state.container.datastore, BACKUP,
        service=settings.SERVICE_NAME, version=settings.SERVICE_VERSION,
        taken_at=_now_iso(), seal=seal, tenant=body.tenant,
        scan_cap=settings.BACKUP_MAX_ROWS_PER_COLLECTION)


@router.post("/backup/verify", dependencies=[Depends(require_internal_token)])
async def backup_verify(request: Request, body: BackupVerifyIn,
                        settings: Settings = Depends(get_settings)) -> dict:
    """Prove an archive restores, by test-restoring it into a throwaway namespace and comparing."""
    import secrets
    _, unseal = _sealer(settings)
    try:
        return await _backup.verify_restore(
            request.app.state.container.datastore, body.archive,
            unseal=unseal, token=secrets.token_hex(8), declaration=BACKUP)
    except _backup.BackupError as exc:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, str(exc)) from exc


async def _reindex(container, scan_cap: int) -> dict[str, int]:
    """Rebuild the two lookup indexes from their source collections — they are DERIVED, not archived.

    users_by_username keys on tenant::normalized-username; the normalization is owned by the
    configured login identifier (email vs username), so it is recomputed here exactly as create()
    does. persons_by_identifier keys on the raw identifier. Both are idempotent and self-healing —
    a re-run recomputes the same rows, and an index that drifted from its source is repaired.
    """
    datastore = container.datastore
    users = persons = 0
    for row in await datastore.scan_all("users", scan_cap):
        uid, tenant, username = row.get("id"), row.get("tenant", ""), row.get("username")
        if not uid or not username:
            continue
        await datastore.put("users_by_username",
                            f"{tenant}::{container.login_identifier.normalize(username)}", {"id": uid})
        users += 1
    for row in await datastore.scan_all("persons", scan_cap):
        pid, identifier = row.get("id"), row.get("identifier")
        if not pid or not identifier:
            continue
        await datastore.put("persons_by_identifier", identifier, {"id": pid})
        persons += 1
    return {"users_by_username": users, "persons_by_identifier": persons}


@router.post("/backup/import", dependencies=[Depends(require_internal_token)])
async def backup_import(request: Request, body: BackupImportIn,
                        settings: Settings = Depends(get_settings)) -> dict:
    """Restore an archive this service produced. Idempotent; leaves newer rows alone by default.

    After the accounts are restored, the username/identifier lookups are rebuilt from them — they
    are derived, not archived, so a restore that did not rebuild them would leave every login
    lookup answering "no such user" against accounts that plainly exist.
    """
    container = request.app.state.container
    _, unseal = _sealer(settings)
    try:
        result = await _backup.import_archive(
            container.datastore, body.archive,
            unseal=unseal, overwrite=body.overwrite, into_tenant=body.into_tenant,
            declaration=BACKUP)
    except _backup.BackupError as exc:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, str(exc)) from exc
    result["reindexed"] = await _reindex(container, settings.BACKUP_MAX_ROWS_PER_COLLECTION)
    return result
