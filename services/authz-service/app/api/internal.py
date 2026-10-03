# =============================================================================
# authz-service — Internal tier (service-to-service, X-Internal-Token)
# -----------------------------------------------------------------------------
# NOT user-facing. Guarded by a shared token compared in CONSTANT TIME
# (CWE-208: observable timing discrepancy). Never a user JWT.
# =============================================================================
from __future__ import annotations


from fastapi import APIRouter, Depends, Header, HTTPException, Query, Request, status
from pydantic import BaseModel, Field

from app.api.v1.authz import build_authz
from app.backup_manifest import BACKUP
from app.core.authz_service import UnknownRole
from app.config import Settings, get_settings
from app.core import backup as _backup
from app.core.cap_policy import forbidden_caps
from app.core.erasure import AuthzErasure
from app.manifest import build_manifest
from app.core.pii import subject_ref

router = APIRouter(prefix="/internal", tags=["internal"])


class CheckIn(BaseModel):
    subject: str = Field(default="", max_length=256)
    action: str = Field(min_length=1, max_length=256)
    resource: str = Field(default="", max_length=512)
    tenant: str = Field(default="", max_length=128)
    extra_caps: list[str] = Field(default_factory=list)
    attributes: dict = Field(default_factory=dict)

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
    # Return the resolved caller so a mutation route can attribute its audit to WHO called (authz-015).
    # Bare `Depends(require_internal_token)` users ignore it; this is backward-compatible.
    return identity.caller


@router.get("/health", dependencies=[Depends(require_internal_token)])
async def internal_health(settings: Settings = Depends(get_settings)) -> dict[str, str]:
    return {"status": "ok", "service": settings.SERVICE_NAME}


@router.post("/authz/check", dependencies=[Depends(require_internal_token)])
async def internal_check(request: Request, body: CheckIn) -> dict:
    """PDP — can `subject` do `action` on `resource`? Returns {allow, reason, matched_policy}."""
    decision = await build_authz(request).check(
        subject=body.subject, action=body.action, resource=body.resource, tenant=body.tenant,
        extra_caps=body.extra_caps, attributes=body.attributes)
    return decision.to_dict()


@router.get("/authz/effective", dependencies=[Depends(require_internal_token)])
async def internal_effective(request: Request, subject: str = Query(...),
                             tenant: str = Query(default=""),
                             roles: str = Query(default="")) -> dict:
    """Resolve a subject's effective capabilities (for identity to embed in tokens).

    `roles` is a comma-separated list of the roles the CALLER holds for this subject — identity
    owns who holds which role (it is a fact about employment, and it lives on the membership row);
    authz owns what a role MEANS. Identity passes the roles in; we expand them.

    Without the `roles` input this endpoint could only expand roles assigned separately INTO authz,
    which nothing does — so it would return the empty set for every real user on the platform, and
    a caller that trusted it would mint tokens with no capabilities at all.
    """
    held = [r.strip() for r in roles.split(",") if r.strip()]
    try:
        caps = await build_authz(request).effective(subject, tenant, roles=held)
    except UnknownRole as exc:
        # EMPTY MUST MEAN "HE HOLDS NONE" — NEVER "I DID NOT UNDERSTAND YOU".
        #
        # Returning 200 with [] for a role we could not resolve is a correct answer to a question
        # asked in the wrong vocabulary. Identity would then mint a token that can do nothing, and
        # every screen would 403 with no error anywhere explaining why. Identity fails CLOSED on
        # this 400, so the operator sees a refusal instead of a user who mysteriously cannot work.
        raise HTTPException(get_settings().AUTHZ_UNKNOWN_ROLE_STATUS, f"UNKNOWN_ROLE: {exc}")
    return {"subject": subject, "tenant": tenant, "capabilities": sorted(caps)}


@router.get("/authz/assignments", dependencies=[Depends(require_internal_token)])
async def internal_assignments(request: Request,
                               tenant: str = Query(..., min_length=1, max_length=128)) -> dict:
    """Every subject's roles + direct capabilities for one tenant — for the (cross-repo) staff/access
    dispute report ("who on this shop holds what"). Complements /authz/effective (one subject): this
    LISTS the whole tenant. Read-only, internal-token, tenant REQUIRED and scoped; current state only
    (assignment rows carry no history). Not a decision path — the PDP still resolves per subject."""
    rows = await build_authz(request).list_assignments(tenant)
    return {"tenant": tenant, "assignments": rows, "count": len(rows)}


class BootstrapRoleIn(BaseModel):
    name: str = Field(min_length=1, max_length=128)
    capabilities: list[str] = Field(default_factory=list)
    description: str = ""


class BootstrapIn(BaseModel):
    """Everything needed to make a brand-new tenant usable: the role definitions to create, the first
    subject (the owner), and which of those roles to grant them."""
    roles: list[BootstrapRoleIn] = Field(default_factory=list)
    subject: str = Field(min_length=1, max_length=256)
    assign: list[str] = Field(default_factory=list)   # role NAMES (subset of roles) to grant `subject`


@router.post("/authz/tenants/{tenant}/bootstrap")
async def bootstrap_tenant(request: Request, tenant: str, body: BootstrapIn,
                           caller: str = Depends(require_internal_token)) -> dict:
    """Give a new tenant its first roles and make its owner an admin — the counterpart to
    POST /internal/tenants, run in the same provisioning saga, at the same internal trust boundary.

    THE CLOSED CYCLE THIS BREAKS: a role granting `authz.manage` can only be created by a holder of
    `authz.manage`, and a fresh tenant has none — so self-service signup mints a token with `caps:[]`
    and the owner can do nothing. This route is the ONE way in, and it is deliberately INTERNAL-tier
    (X-Internal-Token, never a user JWT): the escalation guard on the user-tier routes stays exactly
    as strict as before.

    STILL FAIL-CLOSED ON PLATFORM POWER: it may grant tenant-scoped admin (`authz.manage`) but NEVER
    a `platform.*` capability — a product provisioning a shop must not be able to mint a platform-god
    role. That half of the guard is kept.

    IDEMPOTENT: re-running is a no-op that corrects drift. A role is matched by name (created once,
    updated in place after), and the assignment is a SET — so the whole saga can retry safely.
    `_subject_caps` resolves an assignment's roles BY ID, so the owner is assigned the role IDs, and
    the caps flow into the very next token identity mints from GET /internal/authz/effective.
    """
    s = get_settings()
    # Keep the platform.* denylist (the escalation guard); allow authz.manage — that is the whole
    # point, and only reachable here, at the internal tier.
    for r in body.roles:
        bad = forbidden_caps(r.capabilities, is_operator=False,
                             denied_prefixes=s.authz_tenant_denied_cap_prefixes,
                             denied_caps=frozenset(), allowlist=frozenset())
        if bad:
            raise HTTPException(status.HTTP_400_BAD_REQUEST,
                                f"bootstrap may not grant platform-scoped capabilities: {', '.join(bad)}")

    await request.app.state.auditor.record(
        "authz.tenant_bootstrapped", actor=caller or "", tenant=tenant, resource=f"tenant:{tenant}",
        ip=request.client.host if request.client else "")

    svc = build_authz(request)
    existing = {r["name"]: r for r in await svc.list_roles(tenant)}
    made: list[dict] = []
    name_to_id: dict[str, str] = {}
    for rdef in body.roles:
        if rdef.name in existing:                     # idempotent: update in place, keep the id
            role = await svc.get_role(existing[rdef.name]["id"])
            role = await svc.update_role(role, capabilities=rdef.capabilities,
                                         description=rdef.description or role.description)
            made.append({"id": role.id, "name": role.name, "created": False})
        else:
            role = await svc.create_role(tenant, rdef.name, rdef.capabilities, rdef.description)
            made.append({"id": role.id, "name": role.name, "created": True})
        name_to_id[rdef.name] = role.id

    assign_ids: list[str] = []
    for name in body.assign:
        if name not in name_to_id:
            raise HTTPException(status.HTTP_400_BAD_REQUEST,
                                f"assign references a role not defined in this request: {name!r}")
        assign_ids.append(name_to_id[name])
    if assign_ids:
        await svc.set_subject_roles(body.subject, tenant, roles=assign_ids)

    return {"tenant": tenant, "subject": body.subject, "roles": made, "assigned": body.assign}


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


@router.post("/privacy/erase")
async def erase_subject(request: Request, body: EraseIn,
                        caller: str = Depends(require_internal_token)) -> dict:
    """Erase a data subject. Called by compliance-service during a DSAR fan-out.

    Returns the ack: what was erased, and what was RETAINED with its legal basis. Idempotent —
    a DSAR retries, so erasing an already-erased subject must not fail.
    """
    # Write-ahead: a critical action the log cannot take must not happen at all, so this
    # runs BEFORE the mutation. Recording afterwards would 503 a caller whose action had
    # already succeeded, and leave it unrecorded.
    await request.app.state.auditor.record(
        "subject.erased", actor=caller or "", tenant=body.tenant, resource=f"subject:{subject_ref(get_settings().PII_SALT, body.tenant, body.subject)}",
        ip=request.client.host if request.client else "")
    c = request.app.state.container
    s = get_settings()
    eraser = AuthzErasure(store=c.datastore, pii_salt=s.PII_SALT)
    result = await eraser.erase(body.tenant, body.subject)
    # Invalidate the tenant's decision cache (authz-009): erase deletes assignment rows directly, so
    # without this bump the revoked caps would be served from cache until the TTL elapses.
    if not result.get("already_absent"):
        await build_authz(request).invalidate_tenant(body.tenant)
    return result


# ── Backup (backup-service orchestrates; this service produces sealed archives) ──────────────
# What authz-service holds is declared in app/backup_manifest.py; how it is read lives in
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
    tenant: str = ""


class BackupVerifyIn(BaseModel):
    archive: dict


class BackupImportIn(BaseModel):
    archive: dict
    overwrite: bool = False
    into_tenant: str = ""
    # AUTHORITATIVE undo: also delete rows present now but absent from the archive
    # (backup-r2-01). Set only by the undo path; never combined with into_tenant.
    prune: bool = False


@router.post("/backup/export", dependencies=[Depends(require_internal_token)])
async def backup_export(request: Request, body: BackupExportIn,
                        settings: Settings = Depends(get_settings)) -> dict:
    """Produce a sealed, checksummed archive of the roles, policies, and assignments."""
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


@router.post("/backup/import")
async def backup_import(request: Request, body: BackupImportIn,
                        settings: Settings = Depends(get_settings),
                        caller: str = Depends(require_internal_token)) -> dict:
    """Restore an archive this service produced. Idempotent; leaves newer rows alone by default."""
    _, unseal = _sealer(settings)
    # Write-ahead audit (FIN authz-004): import can OVERWRITE/PRUNE the entire authz state — roles,
    # policies, assignments — which is the most privilege-relevant mutation this service has, yet it was
    # the one mutation with no trail (every role/policy/assignment write is audited). Record it before
    # the restore, like every other critical action, so a state rewrite is never silent.
    await request.app.state.auditor.record(
        "authz.backup_imported", actor=caller or "", tenant=str(body.into_tenant or ""),
        resource="authz:backup",
        ip=request.client.host if request.client else "",
        metadata={"overwrite": str(body.overwrite), "prune": str(body.prune),
                  "into_tenant": str(body.into_tenant or "")})
    try:
        return await _backup.import_archive(
            request.app.state.container.datastore, body.archive,
            unseal=unseal, overwrite=body.overwrite, into_tenant=body.into_tenant,
            declaration=BACKUP, prune=body.prune,
            scan_cap=settings.BACKUP_MAX_ROWS_PER_COLLECTION)
    except _backup.BackupError as exc:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, str(exc)) from exc
