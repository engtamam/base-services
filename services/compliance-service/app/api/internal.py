# =============================================================================
# compliance-service — Internal tier (service-to-service, X-Internal-Token)
# -----------------------------------------------------------------------------
# NOT user-facing. Guarded by a shared token compared in CONSTANT TIME
# (CWE-208: observable timing discrepancy). Never a user JWT.
# =============================================================================
from __future__ import annotations

import hmac

from fastapi import APIRouter, Depends, Header, HTTPException, Request, status
from pydantic import BaseModel, Field

from app.api.v1.compliance import build_consent, build_dsar
from app.config import Settings, get_settings
from app.manifest import build_manifest

router = APIRouter(prefix="/internal", tags=["internal"])


class AckIn(BaseModel):
    service: str = Field(min_length=1, max_length=128)
    category: str = Field(min_length=1, max_length=128)

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


@router.get("/compliance/consent-check", dependencies=[Depends(require_internal_token)])
async def consent_check(request: Request, tenant: str, subject: str, purpose: str) -> dict:
    """HOT PATH — any service asks before processing personal data. Default DENY, cached."""
    return await build_consent(request).check(tenant=tenant, subject=subject, purpose=purpose)


@router.post("/compliance/dsar/{dsar_id}/target-ack", dependencies=[Depends(require_internal_token)])
async def target_ack(request: Request, dsar_id: str, body: AckIn) -> dict:
    """A service confirms it erased/exported its slice. Completion is derived from these."""
    res = await build_dsar(request).ack_target(dsar_id, body.service, body.category)
    if not res["ok"]:
        raise HTTPException(404 if res["reason"] == "NOT_FOUND" else 400, res["reason"])
    return res


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
