# =============================================================================
# authz-service — Internal tier (service-to-service, X-Internal-Token)
# -----------------------------------------------------------------------------
# NOT user-facing. Guarded by a shared token compared in CONSTANT TIME
# (CWE-208: observable timing discrepancy). Never a user JWT.
# =============================================================================
from __future__ import annotations

import hmac

from fastapi import APIRouter, Depends, Header, HTTPException, Query, Request, status
from pydantic import BaseModel, Field

from app.api.v1.authz import build_authz
from app.core.authz_service import UnknownRole
from app.config import Settings, get_settings
from app.core.erasure import AuthzErasure
from app.manifest import build_manifest

router = APIRouter(prefix="/internal", tags=["internal"])


class CheckIn(BaseModel):
    subject: str = Field(default="", max_length=256)
    action: str = Field(min_length=1, max_length=256)
    resource: str = Field(default="", max_length=512)
    tenant: str = Field(default="", max_length=128)
    extra_caps: list[str] = Field(default_factory=list)
    attributes: dict = Field(default_factory=dict)

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

    Returns the ack: what was erased, and what was RETAINED with its legal basis. Idempotent —
    a DSAR retries, so erasing an already-erased subject must not fail.
    """
    c = request.app.state.container
    s = get_settings()
    eraser = AuthzErasure(store=c.datastore, pii_salt=s.PII_SALT)
    return await eraser.erase(body.tenant, body.subject)
