"""Internal payments API — the consumer platform (e.g. the store app) drives the conduit here.

All routes require the internal service token. Credentials are WRITE-ONLY at this boundary: a route
may SET, CLEAR or list HINTS of a shop's MERCHANT_* secrets, and there is deliberately NO route that
returns a stored value — the write-only guarantee the sealed store depends on lives here.
"""
from __future__ import annotations

from fastapi import APIRouter, Depends, Request
from pydantic import BaseModel, Field
from starlette.responses import JSONResponse

from app.api.internal import require_internal_token
from app.api.payments_deps import build_merchant_secrets, build_payment_service
from app.core.merchant_credentials import SETTABLE_NAMES
from app.core.payments import GatewayUnavailable, PaymentNotConfigured, PaymentsDisabled
from app.core.secret_store import SecretError

router = APIRouter(prefix="/internal/payments", tags=["internal-payments"])


# ── merchant credentials (write-only) ─────────────────────────────────────────
class _CredIn(BaseModel):
    tenant: str = Field(min_length=1, max_length=128)
    name: str = Field(min_length=1, max_length=64)
    value: str = Field(min_length=1, max_length=4096)
    actor_ref: str = Field(default="", max_length=128)


class _CredKey(BaseModel):
    tenant: str = Field(min_length=1, max_length=128)
    name: str = Field(min_length=1, max_length=64)


def _reject_unknown(name: str) -> JSONResponse | None:
    if name not in SETTABLE_NAMES:
        return JSONResponse(
            {"ok": False, "reason": f"unknown credential name; allowed: {list(SETTABLE_NAMES)}"},
            status_code=422)
    return None


@router.post("/merchant-credentials", dependencies=[Depends(require_internal_token)])
async def set_credential(request: Request, body: _CredIn) -> dict:
    """Seal one MERCHANT_* credential for a shop. Returns a HINT (masked tail) — never the value."""
    bad = _reject_unknown(body.name)
    if bad is not None:
        return bad
    secrets = build_merchant_secrets(request)
    try:
        info = await secrets.set(tenant=body.tenant, name=body.name, value=body.value,
                                 actor_ref=body.actor_ref)
    except SecretError as exc:
        return JSONResponse({"ok": False, "reason": str(exc)}, status_code=422)
    return {"ok": True, "tenant": body.tenant, "name": info.name, "hint": info.hint,
            "rotating": info.rotating}


@router.post("/merchant-credentials/clear", dependencies=[Depends(require_internal_token)])
async def clear_credential(request: Request, body: _CredKey) -> dict:
    secrets = build_merchant_secrets(request)
    cleared = await secrets.clear(tenant=body.tenant, name=body.name)
    return {"ok": True, "cleared": cleared}


@router.get("/merchant-credentials", dependencies=[Depends(require_internal_token)])
async def list_credentials(request: Request, tenant: str) -> dict:
    """Hints only — which MERCHANT_* names are set for this tenant, masked. No values ever."""
    secrets = build_merchant_secrets(request)
    infos = await secrets.list_info(tenant=tenant)
    return {"tenant": tenant, "credentials": [
        {"name": i.name, "hint": i.hint, "set_at": i.set_at, "rotating": i.rotating} for i in infos]}


# ── payment requests ──────────────────────────────────────────────────────────
class _CreateIn(BaseModel):
    tenant: str = Field(min_length=1, max_length=128)
    reference: str = Field(min_length=1, max_length=256)     # the shop's order id (idempotency key)
    amount_minor: int = Field(gt=0)
    currency: str = Field(min_length=3, max_length=3)
    return_url: str = Field(default="", max_length=2048)
    metadata: dict = Field(default_factory=dict)


@router.post("/requests", dependencies=[Depends(require_internal_token)])
async def create_request(request: Request, body: _CreateIn) -> dict:
    """Mint (or re-use) a hosted pay-link for one order via the shop's own gateway."""
    svc = build_payment_service(request)
    try:
        link = await svc.create_link(
            tenant=body.tenant, reference=body.reference, amount_minor=body.amount_minor,
            currency=body.currency.upper(), metadata=body.metadata, return_url=body.return_url)
    except PaymentNotConfigured as exc:
        return JSONResponse({"ok": False, "reason": str(exc), "code": "NOT_CONFIGURED"}, status_code=409)
    except PaymentsDisabled as exc:
        return JSONResponse({"ok": False, "reason": str(exc), "code": "DISABLED"}, status_code=403)
    except GatewayUnavailable as exc:
        return JSONResponse({"ok": False, "reason": str(exc), "code": "GATEWAY_UNAVAILABLE"},
                            status_code=502)
    return {"ok": True, "request_id": link.request_id, "pay_url": link.pay_url,
            "redirect_url": link.redirect_url, "expires_at": link.expires_at,
            "provider": link.provider, "provider_ref": link.provider_ref}


def _public_view(row: dict) -> dict:
    """A request as an internal caller may see it — no credential, no gateway link internals."""
    return {"request_id": row.get("id"), "tenant": row.get("tenant"),
            "reference": row.get("reference"), "status": row.get("status"),
            "amount_minor": row.get("amount_minor"), "currency": row.get("currency"),
            "provider": row.get("provider"), "provider_ref": row.get("provider_ref"),
            "amount_mismatch": row.get("amount_mismatch", False),
            "expires_at": row.get("expires_at")}


@router.get("/requests/{request_id}", dependencies=[Depends(require_internal_token)])
async def get_request(request: Request, request_id: str, tenant: str) -> dict:
    svc = build_payment_service(request)
    row = await svc._req.get(request_id)
    if row is None or row.get("tenant") != tenant:
        return JSONResponse({"ok": False, "reason": "not found"}, status_code=404)
    return {"ok": True, **_public_view(row)}


@router.post("/requests/{request_id}/reconcile", dependencies=[Depends(require_internal_token)])
async def reconcile_request(request: Request, request_id: str, tenant: str) -> dict:
    """Pull the gateway's current status for a request whose webhook was missed, apply idempotently."""
    svc = build_payment_service(request)
    try:
        row = await svc.reconcile(tenant=tenant, request_id=request_id)
    except GatewayUnavailable as exc:
        return JSONResponse({"ok": False, "reason": str(exc), "code": "GATEWAY_UNAVAILABLE"},
                            status_code=502)
    if row is None:
        return JSONResponse({"ok": False, "reason": "not found"}, status_code=404)
    return {"ok": True, **_public_view(row)}
