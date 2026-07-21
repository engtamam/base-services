# =============================================================================
# zatca-service — admin (PAP) routes: settings, onboarding, invoices, chain, resubmit
# =============================================================================
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request, status
from pydantic import BaseModel, Field
from starlette.responses import Response

from app.api.auth import Principal
from app.api.authz import require_any_capability
from app.config import get_settings
from app.core.stores import InvoiceStore, SettingsStore
from app.core.zatca_service import ZatcaService
from app.ports.zatca_signer import SignerUnavailable

router = APIRouter(prefix="/api/v1/zatca", tags=["zatca"])

_ADMIN = require_any_capability("platform.zatca", "zatca.manage")


async def build_zatca(request: Request, tenant: str = "") -> ZatcaService:
    """Assemble the service for this request.

    Async because the signer credential is resolved from what an operator may have set after
    launch, rather than captured once at boot. The resolver caches, so this is a dict lookup on all
    but the first call in each refresh window.
    """
    c = request.app.state.container
    s = get_settings()
    signer = c.zatca_signer
    creds = getattr(request.app.state, "signer_credentials", None)
    if creds is not None:
        from app.container import rebuild_signer
        signer = rebuild_signer(s, await creds.token(tenant), c.zatca_signer)
    return ZatcaService(
        invoices=InvoiceStore(c.datastore), settings=SettingsStore(c.datastore, s.seal_key),
        signer=signer, fatoora=c.fatoora_client, qr=c.qr_renderer,
        vat_rate=s.VAT_RATE, currency=s.CURRENCY,
        retry={"base_ms": s.ZATCA_RETRY_BASE_MS, "max_ms": s.ZATCA_RETRY_MAX_MS,
               "max_attempts": s.ZATCA_RETRY_MAX_ATTEMPTS}, list_limit=s.ZATCA_LIST_LIMIT,
        validate_before_submit=s.ZATCA_VALIDATE_BEFORE_SUBMIT)


def op_tenant(principal: Principal, body_tenant: str | None = None) -> str:
    if "platform.zatca" in principal.caps:
        return body_tenant or principal.tenant or "global"
    return principal.tenant or "__none__"


class SettingsIn(BaseModel):
    enabled: bool | None = None
    environment: str | None = Field(default=None, pattern="^(sandbox|simulation|production)$")
    seller_name: str | None = None
    seller_vat: str | None = None
    seller_address: str | None = None
    tenant: str | None = None


class OnboardIn(BaseModel):
    otp: str = Field(min_length=1, max_length=64)
    csr_meta: dict[str, Any] = Field(default_factory=dict)
    tenant: str | None = None


# ── settings ──────────────────────────────────────────────────────────────────
@router.get("/settings")
async def get_settings_route(request: Request, principal: Principal = Depends(_ADMIN)) -> dict:
    svc = (await build_zatca(request))
    return await svc._set.view(op_tenant(principal))   # view never returns CSID material


@router.put("/settings")
async def put_settings(request: Request, body: SettingsIn, principal: Principal = Depends(_ADMIN)) -> dict:
    svc = (await build_zatca(request))
    return await svc._set.put(op_tenant(principal, body.tenant), body.model_dump(exclude={"tenant"}))


@router.post("/onboard/compliance")
async def onboard_compliance(request: Request, body: OnboardIn, principal: Principal = Depends(_ADMIN)) -> dict:
    # Write-ahead: a critical action the log cannot take must not happen at all, so this
    # runs BEFORE the mutation. Recording afterwards would 503 a caller whose action had
    # already succeeded, and leave it unrecorded.
    await request.app.state.auditor.record(
        "zatca.onboarded_compliance", actor=principal.subject, tenant=principal.tenant, resource="csid",
        ip=request.client.host if request.client else "")
    svc = (await build_zatca(request))
    try:
        return await svc.onboard_compliance(op_tenant(principal, body.tenant),
                                            otp=body.otp, csr_meta=body.csr_meta)
    except SignerUnavailable:
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, "SIGNER_UNAVAILABLE")


@router.post("/onboard/production")
async def onboard_production(request: Request, principal: Principal = Depends(_ADMIN),
                             tenant: str | None = None) -> dict:
    """Exchange the compliance CSID for a production CSID (after compliance checks pass)."""
    # Write-ahead: a critical action the log cannot take must not happen at all, so this
    # runs BEFORE the mutation. Recording afterwards would 503 a caller whose action had
    # already succeeded, and leave it unrecorded.
    await request.app.state.auditor.record(
        "zatca.onboarded_production", actor=principal.subject, tenant=principal.tenant, resource="csid",
        ip=request.client.host if request.client else "")
    return await (await build_zatca(request)).onboard_production(op_tenant(principal, tenant))


# ── invoices ──────────────────────────────────────────────────────────────────
@router.get("/invoices")
async def list_invoices(request: Request, needs_attention: bool = False,
                        principal: Principal = Depends(_ADMIN)) -> dict:
    svc = (await build_zatca(request))
    return {"invoices": await svc._inv.list(op_tenant(principal), needs_attention=needs_attention,
                                            limit=get_settings().ZATCA_LIST_LIMIT)}


@router.post("/invoices/{invoice_id}/resubmit")
async def resubmit(request: Request, invoice_id: str, principal: Principal = Depends(_ADMIN)) -> dict:
    svc = (await build_zatca(request))
    inv = await svc._inv.get(invoice_id)
    if inv is None or (
            "platform.zatca" not in principal.caps and inv.tenant != (principal.tenant or "__none__")):
        raise HTTPException(status.HTTP_404_NOT_FOUND, "NOT_FOUND")
    res = await svc.resubmit(invoice_id)
    if not res.get("ok"):
        raise HTTPException(422, res.get("reason", "UNPROCESSABLE"))
    return res


@router.get("/invoices/{invoice_id}/qr")
async def invoice_qr(request: Request, invoice_id: str, data_uri: bool = False,
                     principal: Principal = Depends(_ADMIN)):
    svc = (await build_zatca(request))
    inv = await svc._inv.get(invoice_id)
    if inv is None or (
            "platform.zatca" not in principal.caps and inv.tenant != (principal.tenant or "__none__")):
        raise HTTPException(status.HTTP_404_NOT_FOUND, "NOT_FOUND")
    rendered = svc._qr.render(inv.qr) if inv.qr else None
    if rendered is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "QR_UNAVAILABLE")  # null renderer / no TLV
    content_type, data = rendered
    if data_uri:                                          # embeddable (PDF/in-app) — base64 data-URI
        import base64
        return {"data_uri": f"data:{content_type};base64,{base64.b64encode(data).decode()}",
                "content_type": content_type}
    return Response(content=data, media_type=content_type)


@router.get("/chain/verify")
async def chain_verify(request: Request, principal: Principal = Depends(_ADMIN)) -> dict:
    return await (await build_zatca(request)).verify_chain(op_tenant(principal))


@router.post("/sweep")
async def sweep(request: Request, principal: Principal = Depends(_ADMIN)) -> dict:
    return await (await build_zatca(request)).sweep()
