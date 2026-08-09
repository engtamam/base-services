"""Machine identity — the admin surface (issue/list/rotate/revoke) and the OAuth2 token exchange.

This is the inbound half of an integration: a partner's own code gets a credential OF ITS OWN,
instead of holding a human's password (which dies with the employee), a human's refresh token (same
attribution problem, no independent revocation), or the platform-wide internal token (no tenant
scoping — never an option). connector-service already does the OUTBOUND mirror properly; this closes
the gap.

Standards this surface holds to:
  * OAuth2 client-credentials grant (RFC 6749 §4.4) at POST /api/v1/oauth/token.
  * The client secret is shown ONCE, at issue and rotation, and is stored only as a hash — no other
    route returns it, `safety:no-secret-reads` keeps it that way (NIST SP 800-63B; OWASP ASVS 2.4).
  * Least privilege (ISO 27001 A.8.2; OWASP ASVS 1.4.4): a service account's caps are a SUBSET of the
    issuing human's, checked here, and the tenant comes off the issuer's TOKEN, never the request (T1).
  * Every lifecycle mutation is audited WRITE-AHEAD and CRITICAL: minting, rotating, or revoking a
    credential that can act on the API is a change to who has power, and an unrecorded one is
    indistinguishable from an attacker's (ISO 27001 A.8.15; NIST AU-2; PCI-DSS 10.2).
  * The token endpoint is rate-limited, and a failed exchange is recorded (the brute-force signal),
    the same way a failed human login is — declared, not blocking.
"""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from pydantic import BaseModel, Field

from app.api.auth import Principal, require_cap
from app.api.ratelimit import rate_limit
from app.config import get_settings
from app.core.pii import subject_ref
from app.core.service_accounts import (
    CapEscalation,
    ServiceAccounts,
    ServiceAccountStore,
    grantable_caps,
)

router = APIRouter(prefix="/api/v1", tags=["service-accounts"])


def _enabled() -> None:
    """Machine identity is opt-in. When off, the whole surface 404s — it does not half-exist."""
    if not get_settings().SERVICE_ACCOUNTS_ENABLED:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "SERVICE_ACCOUNTS_DISABLED")


def _store(request: Request) -> ServiceAccountStore:
    return ServiceAccountStore(request.app.state.container.datastore)


def _svc(request: Request) -> ServiceAccounts:
    return ServiceAccounts(_store(request), request.app.state.container.password_hasher)


def _actor(principal: Principal) -> str:
    """A pseudonym for the human who issued/rotated/revoked. An audit row naming the operator is
    still personal data, and it lands in an append-only chain."""
    s = get_settings()
    return subject_ref(s.PII_SALT, principal.tenant or "", principal.subject) if s.PII_SALT else ""


def _manage_dep():
    """Resolved per request, so the capability name is config, not import-time furniture."""
    return require_cap(get_settings().SERVICE_ACCOUNTS_WRITE_CAP)


def _read_dep():
    return require_cap(get_settings().SERVICE_ACCOUNTS_READ_CAP)


async def _caller_tenant(request: Request, dep) -> tuple[Principal, str]:
    """The caller (capability enforced by `dep`) and the tenant they are ACTUALLY in — off the
    token, never off the request. A machine identity always belongs to exactly one tenant."""
    principal: Principal = await dep(request)
    if not principal.tenant:
        # A token with no tenant cannot own a tenant-scoped credential. Refused, not defaulted to a
        # blank tenant that would pool every such account into one bucket.
        raise HTTPException(status.HTTP_403_FORBIDDEN, "NO_TENANT")
    return principal, principal.tenant


# ── admin surface ───────────────────────────────────────────────────────────────
class IssueIn(BaseModel):
    name: str = Field(min_length=1, max_length=128)
    # The capabilities the integration should hold. Each MUST be one the issuer holds (no
    # escalation); platform.*/* are stripped for a non-platform tenant.
    caps: list[str] = Field(default_factory=list, max_length=64)


@router.post("/service-accounts", status_code=status.HTTP_201_CREATED,
             dependencies=[Depends(_enabled), Depends(rate_limit)])
async def issue(request: Request, body: IssueIn) -> dict:
    """Mint a machine identity for THIS tenant. Returns the client_id and the client_secret ONCE.

    The secret is not stored in the clear and appears in no other response — copy it now. Its caps
    can never exceed the issuer's own (least privilege), and the tenant is the issuer's, off the
    token.
    """
    s = get_settings()
    principal, tenant = await _caller_tenant(request, _manage_dep())
    try:
        caps = grantable_caps(body.caps, principal.caps, tenant=tenant,
                              platform_tenant=s.PLATFORM_TENANT)
    except CapEscalation as exc:
        # You cannot grant a machine a power you do not hold. Say which cap, so the operator can fix
        # the request — this is an authorization error about the REQUEST, not about the caller.
        raise HTTPException(status.HTTP_403_FORBIDDEN, f"CAP_NOT_GRANTABLE:{exc.cap}") from exc

    # A ceiling per tenant, counting only live accounts — a bounded read, never a scan.
    active = [r for r in await _store(request).list(tenant, limit=s.SERVICE_ACCOUNTS_MAX_PER_TENANT + 1)
              if r.get("status") == "active"]
    if len(active) >= s.SERVICE_ACCOUNTS_MAX_PER_TENANT:
        raise HTTPException(status.HTTP_409_CONFLICT, "SERVICE_ACCOUNT_LIMIT_REACHED")

    actor = _actor(principal)

    async def _audit(account) -> None:
        # WRITE-AHEAD: the record runs after the client_id exists but BEFORE the account is
        # persisted, so a credential the log cannot record is never created (critical).
        await request.app.state.auditor.record(
            "service_account.issued", actor=actor, tenant=tenant,
            resource=f"service_account:{account.id}",
            ip=request.client.host if request.client else "",
            metadata={"name": body.name, "caps": caps})   # names + caps, never the secret

    account, secret = await _svc(request).issue(
        tenant=tenant, name=body.name, caps=caps, created_by=actor, on_commit=_audit)
    return {
        **account.public(),
        "client_secret": secret,               # shown exactly once
        "grant_type": "client_credentials",
        "token_endpoint": "/api/v1/oauth/token",
    }


@router.get("/service-accounts", dependencies=[Depends(_enabled), Depends(rate_limit)])
async def list_service_accounts(request: Request,
                                limit: int | None = Query(default=None, ge=1)) -> dict:
    """List THIS tenant's service accounts. Never a secret — only client_id, name, caps, status."""
    s = get_settings()
    _, tenant = await _caller_tenant(request, _read_dep())
    cap = min(int(limit or s.SERVICE_ACCOUNTS_PAGE_SIZE), s.SERVICE_ACCOUNTS_MAX_PER_TENANT)
    rows = await _store(request).list(tenant, limit=cap)
    from app.core.service_accounts import ServiceAccount
    return {"service_accounts": [ServiceAccount.from_dict(r).public() for r in rows]}


class RotateIn(BaseModel):
    # How long the PREVIOUS secret keeps working, so a fleet embedding the credential migrates
    # without a flag-day outage. Omitted → the platform default. 0 → an immediate cutover, which is
    # the right choice for a leaked secret (shrink the exposure, do not extend it).
    overlap_seconds: int | None = Field(default=None, ge=0)


@router.post("/service-accounts/{client_id}/rotate",
             dependencies=[Depends(_enabled), Depends(rate_limit)])
async def rotate(request: Request, client_id: str, body: RotateIn | None = None) -> dict:
    """Rotate a service account's secret. The client_id is unchanged; only the secret the caller
    holds must be updated. Returns the new secret ONCE.

    By default the PREVIOUS secret keeps authenticating for an overlap window and then expires ON ITS
    OWN, so a fleet of callers migrates without every one failing the instant rotate is pressed. Pass
    `overlap_seconds: 0` for an immediate cutover — the right response to a leak. The response's
    `previous_secret_expires_at` says when the old secret stops working.
    """
    s = get_settings()
    principal, tenant = await _caller_tenant(request, _manage_dep())
    account = await _store(request).get(client_id)
    if account is None or account.tenant != tenant:
        # A 404 whether it is absent or another tenant's — one tenant may not probe another's
        # machine identities.
        raise HTTPException(status.HTTP_404_NOT_FOUND, "NOT_FOUND")
    overlap = (s.SERVICE_ACCOUNT_ROTATION_OVERLAP_SECONDS
               if body is None or body.overlap_seconds is None else body.overlap_seconds)
    # Write-ahead: rotation OVERWRITES the old secret; an unrecorded rotation is unreconstructable.
    await request.app.state.auditor.record(
        "service_account.rotated", actor=_actor(principal), tenant=tenant,
        resource=f"service_account:{client_id}",
        ip=request.client.host if request.client else "",
        metadata={"overlap_seconds": overlap})
    secret = await _svc(request).rotate(account, overlap_seconds=overlap)
    return {**account.public(), "client_secret": secret}


@router.delete("/service-accounts/{client_id}",
               dependencies=[Depends(_enabled), Depends(rate_limit)])
async def revoke(request: Request, client_id: str) -> dict:
    """Revoke a service account — independently of any human account. No secret authenticates and no
    token mints afterwards; the row is kept so the audit trail still resolves the client_id."""
    principal, tenant = await _caller_tenant(request, _manage_dep())
    account = await _store(request).get(client_id)
    if account is None or account.tenant != tenant:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "NOT_FOUND")
    await request.app.state.auditor.record(
        "service_account.revoked", actor=_actor(principal), tenant=tenant,
        resource=f"service_account:{client_id}",
        ip=request.client.host if request.client else "")
    await _svc(request).revoke(account)
    return {"revoked": True, "client_id": client_id}


# ── OAuth2 token exchange (RFC 6749 §4.4) ─────────────────────────────────────────
class TokenIn(BaseModel):
    grant_type: str = Field(default="client_credentials")
    client_id: str = Field(default="", max_length=128)
    client_secret: str = Field(default="", max_length=512)


@router.post("/oauth/token", dependencies=[Depends(_enabled), Depends(rate_limit)])
async def token(request: Request, body: TokenIn) -> dict:
    """Exchange a client_id + client_secret for an access token (client-credentials grant).

    The token is an ordinary access JWT signed by the SAME issuer that signs human tokens, so every
    downstream service verifies it on the path it already has. Its subject is the "svc_…" client_id —
    visibly a machine in the audit trail — and it carries the account's tenant and capabilities.
    """
    ip = request.client.host if request.client else ""

    async def _failed(reason: str) -> None:
        # A failed exchange is the brute-force signal (RFC 6749 says respond invalid_client). The
        # attempted client_id and the source IP are the actor — the same partial-identifier rule a
        # failed human login follows. Declared, not blocking.
        await request.app.state.auditor.record(
            "service_account.auth_failed", actor=body.client_id or "", tenant="",
            resource="oauth:token", ip=ip, metadata={"reason": reason})

    if body.grant_type != "client_credentials":
        await _failed("unsupported_grant_type")
        # RFC 6749 §5.2 error code, in a shape a standard OAuth2 client understands.
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "unsupported_grant_type")

    account = await _svc(request).authenticate(body.client_id, body.client_secret)
    if account is None:
        await _failed("invalid_client")
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "invalid_client")

    s = get_settings()
    claims = _svc(request).token_claims(account, platform_tenant=s.PLATFORM_TENANT)
    access = request.app.state.container.token_issuer.issue_access(
        account.id, claims, s.SERVICE_ACCOUNT_TOKEN_TTL_SECONDS)
    await _svc(request).touch(account)
    await request.app.state.auditor.record(
        "service_account.token_issued", actor=account.id, tenant=account.tenant,
        resource=f"service_account:{account.id}", ip=ip)
    return {
        "token_type": "Bearer",
        "access_token": access,
        "expires_in": s.SERVICE_ACCOUNT_TOKEN_TTL_SECONDS,
        # No refresh token: a machine re-runs the client-credentials exchange, which is cheaper and
        # safer than a long-lived refresh it would have to store.
    }
