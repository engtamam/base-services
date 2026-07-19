# =============================================================================
# zatca-service — Internal tier (service-to-service, X-Internal-Token)
# -----------------------------------------------------------------------------
# NOT user-facing. Guarded by a shared token compared in CONSTANT TIME
# (CWE-208: observable timing discrepancy). Never a user JWT.
# =============================================================================
from __future__ import annotations

import hmac

from typing import Any

from fastapi import APIRouter, Depends, Header, HTTPException, Request, status
from pydantic import BaseModel, Field

from app.api.v1.zatca import build_zatca
from app.config import Settings, get_settings
from app.core.erasure import ZatcaErasure
from app.manifest import build_manifest

router = APIRouter(prefix="/internal", tags=["internal"])


class FinalizeIn(BaseModel):
    tenant: str = Field(min_length=1, max_length=128)
    invoice: dict[str, Any]   # {number, issued_at, seller, buyer?, lines[], currency?, vat_rate?}

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


@router.post("/zatca/finalize", dependencies=[Depends(require_internal_token)])
async def finalize(request: Request, body: FinalizeIn) -> dict:
    """E-invoice one invoice (any service calls this after settling). Never fails the
    caller — a transient error leaves the invoice retryable ('pending')."""
    if not body.invoice.get("number"):
        raise HTTPException(422, "MISSING_NUMBER")
    return await build_zatca(request).finalize(body.invoice, body.tenant)


@router.post("/zatca/sweep", dependencies=[Depends(require_internal_token)])
async def internal_sweep(request: Request) -> dict:
    return await build_zatca(request).sweep()


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
    """zatca deletes NO legal record — it CANNOT, and the ack says why.

    A signed invoice carries a ZATCA signature over its own bytes and its hash is the next
    invoice's PIH, so altering one invoice would invalidate every invoice after it. Unlike billing,
    the buyer's identity here cannot even be pseudonymised. What IS erased is the unsigned draft
    working copy of already-accepted invoices: real personal data, no legal significance.
    """
    c = request.app.state.container
    s = get_settings()
    eraser = ZatcaErasure(store=c.datastore, pii_salt=s.PII_SALT,
                          retention_years=s.TAX_RETENTION_YEARS, max_scan=s.ERASURE_MAX_SCAN)
    return await eraser.erase(body.tenant, body.subject)
