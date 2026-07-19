# =============================================================================
# verification-service — Internal tier (service-to-service, X-Internal-Token)
# -----------------------------------------------------------------------------
# NOT user-facing. Guarded by a shared token compared in CONSTANT TIME
# (CWE-208: observable timing discrepancy). Never a user JWT.
# =============================================================================
from __future__ import annotations

import hmac

from pydantic import BaseModel

from fastapi import APIRouter, Depends, Header, HTTPException, Request, status

from app.config import Settings, get_settings
from app.core.erasure import VerificationErasure
from app.core.stores import IdentityRegistry, VerificationStore
from app.manifest import build_manifest

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

    Returns the ack: what was erased, and what was RETAINED with its legal basis. Idempotent — a
    DSAR retries, so erasing an already-erased subject must not fail. Wire this service into the
    fan-out by adding it to PRIVACY_TARGETS in compliance-service's values file; the
    safety:privacy-wiring gate fails if you implement this and forget.
    """
    c = request.app.state.container
    s = get_settings()
    eraser = VerificationErasure(store=VerificationStore(c.datastore),
                                 object_store=c.object_store, max_scan=s.ERASURE_MAX_SCAN)
    # Write-ahead: erasure is irreversible, so a record we could not write must stop the deletion
    # rather than follow it. Recording afterwards would 503 a caller whose data was ALREADY gone.
    await request.app.state.auditor.record(
        "subject.erased", tenant=body.tenant, resource=f"subject:{body.subject}")
    return await eraser.erase(body.tenant, body.subject)


@router.get("/verification/identity/{claim_ref}/seen",
            dependencies=[Depends(require_internal_token)])
async def identity_seen(request: Request, claim_ref: str) -> dict:
    """Has this verified identity been seen before?

    The anti-abuse question, asked by onboarding-service before it grants anything once per
    identity. Internal tier because the caller is a SERVICE, not a person — a capability check
    would have been machine auth wearing a user's clothes.

    Takes a claim_ref rather than a raw value on purpose: the caller must already hold the hash, so
    this cannot be used to ask whether a given national id exists in our records. A raw-value
    endpoint here would be an enumeration oracle over a national registry.
    """
    registry = IdentityRegistry(request.app.state.container.datastore)
    return {"seen": await registry.seen(claim_ref)}


@router.post("/verification/sweep-pending", dependencies=[Depends(require_internal_token)])
async def sweep_pending(request: Request) -> dict:
    """Lapse verification prompts nobody answered. Called by the sweep CronJob.

    A safety net, not the authority: status() already refuses to trust a lapsed record, so a sweep
    that has not run cannot leave one trusted. This keeps the store tidy and makes the lapse
    visible, rather than leaving rows that look pending forever.
    """
    from app.core.stores import VerificationStore
    from app.core.verification_service import VerificationService

    c = request.app.state.container
    s = get_settings()
    svc = VerificationService(store=VerificationStore(c.datastore), verifiers=c.verifiers,
                              clock=c.clock, pii_salt=s.PII_SALT,
                              pending_ttl=s.VERIFY_PENDING_TTL_SECONDS)
    return {"lapsed": await svc.sweep_pending()}
