"""Internal native-payment API — order_details "Review and Pay", order status, lookup-verify, refund.

BUILD-AHEAD, KSA-DORMANT. Native pay via a gateway is India-only today; every route here is gated by
WHATSAPP_NATIVE_PAY_ENABLED and returns 503 until a deployment enables it (per-tenant locking is
layered in P6). The order_details/order_status MESSAGES go through the ordinary interactive send path;
lookup + refund are server-to-server Graph calls (the money-carrying half) via the WhatsAppPayments
adapter. A payment webhook is only a hint — the lookup is the source of truth (never trust the webhook
body alone).
"""
from __future__ import annotations

import asyncio

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
    # OFFLOAD the blocking Graph lookup off the event loop (notification-004).
    res = await asyncio.to_thread(adapter.get_payment_status,
                                  configuration=body.payment_configuration,
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
    # notification-013: serialise the lookup+refund pair with a cas() reservation keyed by
    # (tenant, reference_id). The state guard below is a two-call TOCTOU on its own — two concurrent (or
    # one redelivered) refunds both read 'captured' and both move money. A reserve-if-absent lock lets
    # exactly ONE refund proceed per reference; a loser is refused, not repeated. The lock is RELEASED on
    # any path that does not actually call adapter.refund (a failed precondition), so a corrected retry is
    # still possible; it is KEPT once the refund is dispatched, making the money movement once-only.
    ds = request.app.state.container.datastore
    tref = body.tenant or ""
    lock_key = f"{len(tref)}:{tref}:{body.reference_id}"
    reserved = await ds.cas("native_refund_locks", lock_key, 0,
                            {"id": lock_key, "tenant": tref, "reference_id": body.reference_id})
    if not reserved:
        return JSONResponse(
            {"ok": False, "reason": "a refund for this reference is already in progress or completed",
             "code": "REFUND_IN_PROGRESS"}, status_code=409)

    async def _release():
        await ds.delete("native_refund_locks", lock_key)

    adapter = await _payments_adapter(request, settings, body.tenant or "")
    # FAIL-CLOSED money guard against the SOURCE OF TRUTH (the lookup, per this module's own contract)
    # BEFORE releasing money. Without it the refund route trusted the caller's amount/currency blindly:
    # it would over-refund (refund > captured), refund in a currency the payment was never captured in,
    # or double-refund a redelivered request. We verify the payment is actually captured, the currency
    # matches, and the amount does not exceed what was captured. A refunded/partially-refunded payment
    # is no longer "captured", so a replay of a completed refund is refused here — once-only by state.
    lk = await asyncio.to_thread(adapter.get_payment_status,          # off the event loop (notification-004)
                                 configuration=body.payment_configuration,
                                 reference_id=body.reference_id)
    if not lk.ok:
        await _release()
        return JSONResponse(
            {"ok": False, "reason": f"cannot verify payment before refund: {lk.error}",
             "code": "REFUND_LOOKUP_FAILED"}, status_code=502)
    if lk.status != "captured":
        # Not refundable: pending/failed/canceled, or ALREADY (fully/partially) refunded — the latter
        # makes a redelivered refund a no-op refusal instead of a second money movement. Keep the lock:
        # a non-captured status is terminal for this reference, so a retry should stay refused.
        return JSONResponse(
            {"ok": False, "reason": f"payment is not refundable in status '{lk.status}'",
             "code": "PAYMENT_NOT_REFUNDABLE", "status": lk.status}, status_code=409)
    if lk.currency and lk.currency.upper() != body.currency.upper():
        await _release()
        return JSONResponse(
            {"ok": False, "reason": "refund currency does not match the captured payment currency",
             "code": "REFUND_CURRENCY_MISMATCH", "captured_currency": lk.currency.upper()},
            status_code=422)
    if int(lk.amount_minor) <= 0:
        await _release()
        return JSONResponse(
            {"ok": False, "reason": "captured amount is unknown; refusing to refund an unverifiable ceiling",
             "code": "CAPTURED_AMOUNT_UNKNOWN"}, status_code=422)
    # Compare across possibly-different decimal offsets without floats: a/oa <= b/ob  <=>  a*ob <= b*oa.
    if int(body.amount_minor) * int(lk.offset) > int(lk.amount_minor) * int(body.offset):
        await _release()
        return JSONResponse(
            {"ok": False, "reason": "refund amount exceeds the captured amount",
             "code": "REFUND_EXCEEDS_CAPTURED", "captured_minor": int(lk.amount_minor),
             "captured_offset": int(lk.offset)}, status_code=422)
    # WRITE-AHEAD audit the money movement BEFORE releasing it (notification-003): a refund is a
    # privileged financial action and must have a witness naming reference_id + amount (never card
    # data), like every other privileged action here. Idempotency is enforced by STATE above — a
    # completed refund leaves the payment no longer 'captured', so a redelivered call is refused at the
    # PAYMENT_NOT_REFUNDABLE guard rather than moving money twice.
    auditor = getattr(request.app.state, "auditor", None)
    if auditor is not None:
        await auditor.record(
            "whatsapp.payment_refunded", actor="service:internal", tenant=body.tenant or "",
            resource=f"payment:{body.reference_id}",
            ip=request.client.host if request.client else "",
            metadata={"amount_minor": int(body.amount_minor), "currency": body.currency.upper(),
                      "configuration": body.payment_configuration})
    res = await asyncio.to_thread(                     # off the event loop (notification-004)
        adapter.refund, reference_id=body.reference_id, configuration=body.payment_configuration,
        amount_minor=body.amount_minor, currency=body.currency.upper(),
        offset=body.offset, speed=body.speed)
    if not res.ok:
        # The gateway refused — no money moved — so release the reservation to allow a corrected retry.
        await _release()
    return {"ok": res.ok, "refund_id": res.refund_id, "status": res.status, "error": res.error}
