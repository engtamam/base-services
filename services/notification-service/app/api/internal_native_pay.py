"""Internal native-payment API — order_details "Review and Pay", order status, lookup-verify, refund.

BUILD-AHEAD, KSA-DORMANT. Native pay via a gateway is India-only today; every route here is gated by
WHATSAPP_NATIVE_PAY_ENABLED and returns 503 until a deployment enables it (per-tenant locking is
layered in P6). The order_details/order_status MESSAGES go through the ordinary interactive send path;
lookup + refund are server-to-server Graph calls (the money-carrying half) via the WhatsAppPayments
adapter. A payment webhook is only a hint — the lookup is the source of truth (never trust the webhook
body alone).
"""
from __future__ import annotations

from fastapi import APIRouter, Depends, Request
from pydantic import BaseModel, Field
from starlette.responses import JSONResponse

from app.api.internal import require_internal_token
from app.api.v1.notifications import build_notifier
from app.config import Settings, get_settings
from app.core.option_gate import OptionGate
from app.core.whatsapp_interactive import InteractiveError, build_order_details, build_order_status

router = APIRouter(prefix="/internal/whatsapp/pay", tags=["native-pay"])


def _gate(settings: Settings) -> JSONResponse | None:
    if not settings.WHATSAPP_NATIVE_PAY_ENABLED:
        return JSONResponse(
            {"ok": False, "reason": "native WhatsApp pay is disabled (KSA-dormant); set "
             "WHATSAPP_NATIVE_PAY_ENABLED to enable", "code": "NATIVE_PAY_DISABLED"}, status_code=503)
    return None


async def _tenant_gate(request: Request, tenant: str) -> JSONResponse | None:
    """The SECOND gate: even when native pay is available in this deployment (global flag on), a shop
    uses it only if its per-tenant `native_pay` option is enabled. Fail-closed on an empty tenant —
    native pay is always per-shop, so a missing tenant is never "everyone allowed"."""
    gate = OptionGate(request.app.state.container.datastore)
    if not tenant or not await gate.is_enabled(tenant, "native_pay"):
        return JSONResponse(
            {"ok": False, "reason": "native WhatsApp pay is not enabled for this shop",
             "code": "NATIVE_PAY_NOT_ENABLED_FOR_TENANT"}, status_code=403)
    return None


async def _payments_adapter(request: Request, settings: Settings, tenant: str):
    """Build the native-payment adapter for a tenant. meta resolves the tenant's phone id + access
    token from the provider pool (same seam as sending); console is the test/zero-infra double."""
    if settings.WHATSAPP_PAYMENTS_DRIVER == "console":
        from app.adapters.whatsapp_payments.console import ConsoleWhatsAppPayments
        return ConsoleWhatsAppPayments()
    from app.adapters.whatsapp_payments.meta import MetaWhatsAppPayments
    pool = request.app.state.provider_pool
    phone_id = await pool.whatsapp_phone_number_id(tenant)
    token = await pool.whatsapp_access_token(tenant)
    return MetaWhatsAppPayments(token, phone_id, base_url=settings.WHATSAPP_GRAPH_BASE)


class _ItemIn(BaseModel):
    retailer_id: str = Field(min_length=1, max_length=256)
    name: str = Field(min_length=1, max_length=256)
    amount_minor: int = Field(ge=0)
    quantity: int = Field(gt=0)


class _OrderDetailsIn(BaseModel):
    recipient: str = Field(min_length=1, max_length=256)
    tenant: str | None = Field(default=None, max_length=128)
    reference_id: str = Field(min_length=1, max_length=35)
    currency: str = Field(min_length=3, max_length=3)
    order_type: str = Field(default="physical-goods", max_length=32)
    items: list[_ItemIn] = Field(min_length=1)
    subtotal_minor: int = Field(gt=0)
    tax_minor: int = Field(default=0, ge=0)
    shipping_minor: int = Field(default=0, ge=0)
    discount_minor: int = Field(default=0, ge=0)
    payment_gateway: str = Field(min_length=1, max_length=64)
    payment_configuration: str = Field(min_length=1, max_length=128)
    offset: int = Field(default=100)
    body: str = Field(default="", max_length=1024)
    header_image_url: str = Field(default="", max_length=2048)
    footer: str = Field(default="", max_length=60)
    idempotency_key: str | None = Field(default=None, max_length=256)


@router.post("/order-details", dependencies=[Depends(require_internal_token)])
async def send_order_details(request: Request, body: _OrderDetailsIn,
                             settings: Settings = Depends(get_settings)) -> dict:
    gated = _gate(settings)
    if gated is not None:
        return gated
    denied = await _tenant_gate(request, body.tenant or "")
    if denied is not None:
        return denied
    try:
        interactive = build_order_details(
            reference_id=body.reference_id, currency=body.currency.upper(), order_type=body.order_type,
            items=[i.model_dump() for i in body.items], subtotal_minor=body.subtotal_minor,
            tax_minor=body.tax_minor, shipping_minor=body.shipping_minor,
            discount_minor=body.discount_minor, payment_gateway=body.payment_gateway,
            payment_configuration=body.payment_configuration, offset=body.offset, body=body.body,
            header_image_url=body.header_image_url, footer=body.footer)
    except InteractiveError as exc:
        return JSONResponse({"ok": False, "reason": str(exc)}, status_code=422)
    notifier = await build_notifier(request, body.tenant or "")
    return await notifier.send_interactive(
        recipient=body.recipient, tenant=body.tenant or "", interactive=interactive,
        kind="order_details", idempotency_key=body.idempotency_key)


class _OrderStatusIn(BaseModel):
    recipient: str = Field(min_length=1, max_length=256)
    tenant: str | None = Field(default=None, max_length=128)
    reference_id: str = Field(min_length=1, max_length=35)
    status: str = Field(min_length=1, max_length=32)
    description: str = Field(default="", max_length=1024)
    body: str = Field(default="", max_length=1024)
    idempotency_key: str | None = Field(default=None, max_length=256)


@router.post("/order-status", dependencies=[Depends(require_internal_token)])
async def send_order_status(request: Request, body: _OrderStatusIn,
                            settings: Settings = Depends(get_settings)) -> dict:
    gated = _gate(settings)
    if gated is not None:
        return gated
    denied = await _tenant_gate(request, body.tenant or "")
    if denied is not None:
        return denied
    try:
        interactive = build_order_status(reference_id=body.reference_id, status=body.status,
                                         description=body.description, body=body.body)
    except InteractiveError as exc:
        return JSONResponse({"ok": False, "reason": str(exc)}, status_code=422)
    notifier = await build_notifier(request, body.tenant or "")
    return await notifier.send_interactive(
        recipient=body.recipient, tenant=body.tenant or "", interactive=interactive,
        kind="order_status", idempotency_key=body.idempotency_key)


class _LookupIn(BaseModel):
    tenant: str | None = Field(default=None, max_length=128)
    reference_id: str = Field(min_length=1, max_length=35)
    payment_configuration: str = Field(min_length=1, max_length=128)


@router.post("/lookup", dependencies=[Depends(require_internal_token)])
async def lookup_payment(request: Request, body: _LookupIn,
                         settings: Settings = Depends(get_settings)) -> dict:
    """The source of truth for a payment — never trust a type:payment webhook alone; confirm here."""
    gated = _gate(settings)
    if gated is not None:
        return gated
    denied = await _tenant_gate(request, body.tenant or "")
    if denied is not None:
        return denied
    adapter = await _payments_adapter(request, settings, body.tenant or "")
    res = adapter.get_payment_status(configuration=body.payment_configuration,
                                     reference_id=body.reference_id)
    return {"ok": res.ok, "reference_id": res.reference_id, "status": res.status,
            "amount_minor": res.amount_minor, "offset": res.offset, "currency": res.currency,
            "transaction_id": res.transaction_id, "verified": res.verified, "error": res.error}


class _RefundIn(BaseModel):
    tenant: str | None = Field(default=None, max_length=128)
    reference_id: str = Field(min_length=1, max_length=35)
    payment_configuration: str = Field(min_length=1, max_length=128)
    amount_minor: int = Field(gt=0)
    currency: str = Field(min_length=3, max_length=3)
    offset: int = Field(default=100)
    speed: str = Field(default="normal", max_length=16)


@router.post("/refund", dependencies=[Depends(require_internal_token)])
async def refund_payment(request: Request, body: _RefundIn,
                         settings: Settings = Depends(get_settings)) -> dict:
    gated = _gate(settings)
    if gated is not None:
        return gated
    denied = await _tenant_gate(request, body.tenant or "")
    if denied is not None:
        return denied
    adapter = await _payments_adapter(request, settings, body.tenant or "")
    res = adapter.refund(reference_id=body.reference_id, configuration=body.payment_configuration,
                         amount_minor=body.amount_minor, currency=body.currency.upper(),
                         offset=body.offset, speed=body.speed)
    return {"ok": res.ok, "refund_id": res.refund_id, "status": res.status, "error": res.error}
