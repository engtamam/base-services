# =============================================================================
# billing-service — admin (catalog/ops) + customer (subscribe/invoices) routes
# =============================================================================
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request, status
from pydantic import BaseModel, Field
from starlette.responses import Response

from app.api.auth import Principal
from app.api.authz import require_any_capability
from app.config import get_settings
from app.core import settlement as _stl
from app.core.billing_service import BillingService
from app.core.stores import (CouponStore, IdempotencyStore, InvoiceStore, LedgerStore,
                             PaymentStore, PlanStore, SettlementStore, SubscriptionStore,
                             UsageStore)
from app.core.usage_service import UsageService

router = APIRouter(prefix="/api/v1/billing", tags=["billing"])

_ADMIN = require_any_capability("platform.billing", "billing.manage")
_SELF = require_any_capability("billing.self", "billing.manage", "platform.billing")


def build_billing(request: Request) -> BillingService:
    c = request.app.state.container
    s = get_settings()
    return BillingService(
        plans=PlanStore(c.datastore, c.cache), coupons=CouponStore(c.datastore),
        subs=SubscriptionStore(c.datastore, c.cache),
        invoices=InvoiceStore(c.datastore), payments=PaymentStore(c.datastore), usage=UsageStore(c.datastore),
        ledger=LedgerStore(c.datastore, cas_max_retries=s.LEDGER_CAS_MAX_RETRIES,
                           cas_backoff_ms=s.LEDGER_CAS_BACKOFF_MS),
        idempo=IdempotencyStore(c.datastore),
        gateway=c.payment_gateway, tax=c.tax_calculator, einvoicer=c.einvoicer, pdf=c.pdf_renderer,
        cache=c.cache, settlements=SettlementStore(c.datastore),
        config={"currency": s.CURRENCY, "trial_days": s.TRIAL_DAYS, "vat_rate": s.VAT_RATE,
                "reverse_charge": s.REVERSE_CHARGE_B2B, "grandfather": s.GRANDFATHER,
                "dunning_base_hours": s.DUNNING_RETRY_BASE_HOURS, "dunning_max_attempts": s.DUNNING_MAX_ATTEMPTS,
                "grace_days": s.GRACE_DAYS, "list_limit": s.BILLING_LIST_LIMIT,
                "limits_cache_ttl": s.LIMITS_CACHE_TTL_SECONDS,
                "settlement_methods": s.settlement_methods,
                "settlement_allow_partial": s.SETTLEMENT_ALLOW_PARTIAL,
                "settlement_tolerance_minor": s.SETTLEMENT_TOLERANCE_MINOR,
                "settlement_max_backdate_days": s.SETTLEMENT_MAX_BACKDATE_DAYS})


def build_usage(request: Request) -> UsageService:
    """UsageService owns the usage number. Routes never accept one from the caller."""
    c = request.app.state.container
    s = get_settings()
    billing = build_billing(request)
    return UsageService(
        store=c.datastore, limits_resolver=billing.limits_resolver, meter=UsageStore(c.datastore),
        config={"default_period": s.USAGE_DEFAULT_PERIOD,
                "idempotency_ttl": s.USAGE_IDEMPOTENCY_TTL_SECONDS,
                "unknown_key_default": s.ENTITLEMENT_UNKNOWN_KEY_DEFAULT})


def _tenant(p: Principal, body_tenant: str | None = None) -> str:
    if "platform.billing" in p.caps:
        return body_tenant or p.tenant or "global"
    return p.tenant or "__none__"


def _guard(p: Principal, obj_tenant: str) -> None:
    if "platform.billing" not in p.caps and obj_tenant != (p.tenant or "__none__"):
        raise HTTPException(status.HTTP_404_NOT_FOUND, "NOT_FOUND")


class PlanIn(BaseModel):
    name: str = Field(min_length=1, max_length=128)
    prices: list[dict[str, Any]] = Field(min_length=1)
    features: list[str] = Field(default_factory=list)
    limits: dict[str, Any] = Field(default_factory=dict)
    trial_days: int = 0


class CouponIn(BaseModel):
    code: str = Field(min_length=1, max_length=64)
    type: str = Field(default="percent", pattern="^(percent|fixed|trial)$")
    value: int = 0
    max_redemptions: int = 0


class SubscribeIn(BaseModel):
    plan_id: str
    interval: str = Field(default="month", pattern="^(month|year|once)$")
    quantity: int = Field(default=1, ge=1)
    coupon_code: str = ""
    payment_token: str = ""
    buyer_name: str = ""
    buyer_vat: str = ""
    buyer_cr: str = ""
    tenant: str | None = None


class ChangeIn(BaseModel):
    new_plan_id: str
    interval: str | None = Field(default=None, pattern="^(month|year|once)$")
    quantity: int | None = Field(default=None, ge=1)


class UsageIn(BaseModel):
    subscription_id: str
    metric: str = Field(min_length=1)
    quantity: int = Field(ge=0)


# ── catalog (admin) ────────────────────────────────────────────────────────────
@router.post("/plans", status_code=201, dependencies=[Depends(_ADMIN)])
async def create_plan(request: Request, body: PlanIn) -> dict:
    p = await build_billing(request).create_plan(**body.model_dump())
    return p.to_dict()


@router.get("/plans", dependencies=[Depends(_SELF)])
async def list_plans(request: Request) -> dict:
    return {"plans": await build_billing(request).list_plans()}


@router.post("/coupons", status_code=201, dependencies=[Depends(_ADMIN)])
async def create_coupon(request: Request, body: CouponIn) -> dict:
    return (await build_billing(request).upsert_coupon(**body.model_dump())).to_dict()


# ── subscriptions (customer) ────────────────────────────────────────────────────
@router.post("/subscriptions", status_code=201)
async def subscribe(request: Request, body: SubscribeIn, principal: Principal = Depends(_SELF)) -> dict:
    try:
        sub = await build_billing(request).subscribe(
            tenant=_tenant(principal, body.tenant), plan_id=body.plan_id, interval=body.interval,
            quantity=body.quantity, coupon_code=body.coupon_code, payment_token=body.payment_token,
            buyer_name=body.buyer_name, buyer_vat=body.buyer_vat, buyer_cr=body.buyer_cr)
    except ValueError as e:
        raise HTTPException(status.HTTP_404_NOT_FOUND, str(e))
    return sub.to_dict()


@router.post("/subscriptions/{sub_id}/change")
async def change_plan(request: Request, sub_id: str, body: ChangeIn,
                      principal: Principal = Depends(_SELF)) -> dict:
    svc = build_billing(request)
    sub = await svc._subs.fetch(sub_id)
    if sub is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "NOT_FOUND")
    _guard(principal, sub.tenant)
    try:
        return await svc.change_plan(sub_id, new_plan_id=body.new_plan_id,
                                     interval=body.interval, quantity=body.quantity)
    except ValueError as e:
        raise HTTPException(status.HTTP_404_NOT_FOUND, str(e))


@router.post("/subscriptions/{sub_id}/pause")
async def pause(request: Request, sub_id: str, principal: Principal = Depends(_SELF)) -> dict:
    return await _transition(request, sub_id, principal, "pause")


@router.post("/subscriptions/{sub_id}/reactivate")
async def reactivate(request: Request, sub_id: str, principal: Principal = Depends(_SELF)) -> dict:
    return await _transition(request, sub_id, principal, "reactivate")


async def _transition(request: Request, sub_id: str, principal: Principal, action: str) -> dict:
    """Shared because the two differ only in which method they call — and because the tenant
    guard is the part that must not be written twice and then written slightly differently.

    409, not 404, on an illegal move: the subscription exists and the caller may not do this to
    it. A 404 would send them off to create a duplicate.
    """
    svc = build_billing(request)
    sub = await svc._subs.fetch(sub_id)
    if sub is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "NOT_FOUND")
    _guard(principal, sub.tenant)
    try:
        updated = await getattr(svc, action)(sub_id)
    except ValueError as e:
        raise HTTPException(status.HTTP_409_CONFLICT, str(e))
    return updated.to_dict()


@router.post("/subscriptions/{sub_id}/cancel")
async def cancel(request: Request, sub_id: str, at_period_end: bool = True,
                 principal: Principal = Depends(_SELF)) -> dict:
    svc = build_billing(request)
    sub = await svc._subs.fetch(sub_id)
    if sub is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "NOT_FOUND")
    _guard(principal, sub.tenant)
    return (await svc.cancel(sub_id, at_period_end=at_period_end)).to_dict()


@router.post("/subscriptions/{sub_id}/run", dependencies=[Depends(_ADMIN)])
async def run_cycle(request: Request, sub_id: str) -> dict:
    try:
        return (await build_billing(request).run_cycle(sub_id)).to_dict()
    except ValueError as e:
        raise HTTPException(status.HTTP_404_NOT_FOUND, str(e))


@router.get("/subscriptions", dependencies=[Depends(_SELF)])
async def list_subs(request: Request, principal: Principal = Depends(_SELF)) -> dict:
    return {"subscriptions": await build_billing(request)._subs.list(_tenant(principal),
                                                                     get_settings().BILLING_LIST_LIMIT)}


@router.post("/usage", status_code=201)
async def record_usage(request: Request, body: UsageIn, principal: Principal = Depends(_SELF)) -> dict:
    return await build_billing(request).record_usage(
        tenant=_tenant(principal), subscription_id=body.subscription_id,
        metric=body.metric, quantity=body.quantity)


# ── invoices ───────────────────────────────────────────────────────────────────
@router.get("/invoices", dependencies=[Depends(_SELF)])
async def list_invoices(request: Request, principal: Principal = Depends(_SELF)) -> dict:
    return {"invoices": await build_billing(request)._invoices.list(_tenant(principal),
                                                                    get_settings().BILLING_LIST_LIMIT)}


@router.get("/invoices/{invoice_id}/pdf")
async def invoice_pdf(request: Request, invoice_id: str, principal: Principal = Depends(_SELF)):
    svc = build_billing(request)
    inv = await svc._invoices.fetch(invoice_id)
    if inv is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "NOT_FOUND")
    _guard(principal, inv.tenant)
    rendered = svc.render_pdf(inv.to_dict())
    if rendered is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "PDF_UNAVAILABLE")
    ct, data = rendered
    return Response(content=data, media_type=ct,
                    headers={"Content-Disposition": f'attachment; filename="{inv.number}.pdf"'})


@router.post("/payments/{payment_id}/refund", dependencies=[Depends(_ADMIN)])
async def refund(request: Request, payment_id: str, amount_minor: int | None = None) -> dict:
    # Write-ahead: a critical action the log cannot take must not happen at all, so this
    # runs BEFORE the mutation. Recording afterwards would 503 a caller whose action had
    # already succeeded, and leave it unrecorded.
    await request.app.state.auditor.record(
        "payment.refunded", actor="", tenant="", resource=f"payment:{payment_id}",
        ip=request.client.host if request.client else "")
    res = await build_billing(request).refund(payment_id, amount_minor)
    if not res.get("ok") and res.get("reason") == "NOT_FOUND":
        raise HTTPException(status.HTTP_404_NOT_FOUND, "NOT_FOUND")
    return res


@router.post("/dunning/run", dependencies=[Depends(_ADMIN)])
async def dunning(request: Request) -> dict:
    return await build_billing(request).dunning_sweep()


@router.post("/subscriptions/{sub_id}/migrate", dependencies=[Depends(_ADMIN)])
async def migrate_terms(request: Request, sub_id: str) -> dict:
    """Explicitly move a grandfathered subscription onto the CURRENT plan terms."""
    try:
        return await build_billing(request).migrate_terms(sub_id)
    except ValueError as e:
        raise HTTPException(status.HTTP_404_NOT_FOUND, str(e))


@router.post("/subscriptions/{sub_id}/overage", dependencies=[Depends(_ADMIN)])
async def run_overage(request: Request, sub_id: str) -> dict:
    inv = await build_billing(request).run_overage(sub_id)
    return inv.to_dict() if inv else {"overage": 0}


@router.get("/entitlements/{key}", dependencies=[Depends(_SELF)])
async def entitlement(request: Request, key: str, amount: int = 1,
                      principal: Principal = Depends(_SELF)) -> dict:
    """Advisory. Usage is read SERVER-SIDE.

    There is deliberately no `current` parameter any more. It used to be a query string, which
    meant a tenant could answer "how much have you used?" with "none" and pass every limit
    (CWE-602: client-side enforcement of a server-side security decision). Enforcement lives
    in POST /internal/billing/usage/consume, which is atomic.
    """
    return await build_usage(request).check(tenant=_tenant(principal), key=key, amount=amount)


@router.get("/usage", dependencies=[Depends(_SELF)])
async def usage_summary(request: Request, principal: Principal = Depends(_SELF)) -> dict:
    """'18 of 25 seats'. Without this the client has to count, which is how it went wrong."""
    return await build_usage(request).summary(_tenant(principal))


@router.get("/ledger", dependencies=[Depends(_ADMIN)])
async def ledger(request: Request, principal: Principal = Depends(_ADMIN)) -> dict:
    return {"ledger": await build_billing(request)._ledger.list(_tenant(principal),
                                                               get_settings().BILLING_LIST_LIMIT)}


@router.get("/gateway/verify", dependencies=[Depends(_ADMIN)])
async def gateway_verify(request: Request) -> dict:
    """Confirm which gateway is wired WITHOUT charging (driver + live/recording mode)."""
    gw = request.app.state.container.payment_gateway
    return {"driver": gw.driver, "configured": gw.driver not in ("null",),
            "mode": "recording" if gw.driver == "recording" else ("off" if gw.driver == "null" else "live")}


# ── off-gateway settlement (Phase 27) ──────────────────────────────────────────
# Money that never touched a gateway: a wire, a cheque, cash, an offline terminal.
# It flows into the SAME private _mark_paid path a card charge takes, so entitlements,
# ZATCA e-invoicing and dunning behave identically. One truth about money.
class SettleIn(BaseModel):
    method: str = Field(min_length=1, max_length=64)     # from SETTLEMENT_METHODS
    reference: str = Field(min_length=1, max_length=200)
    issuer: str = Field(default="", max_length=200)      # required iff the method says so
    amount_minor: int = Field(gt=0)
    paid_at: float                                       # the BANK's value date, not now()
    currency: str = ""
    actor: str = Field(default="", max_length=200)       # a person, not a service token
    proof_file_id: str = Field(default="", max_length=200)
    note: str = Field(default="", max_length=500)


class ReverseIn(BaseModel):
    reason: str = Field(default="", max_length=500)
    actor: str = Field(default="", max_length=200)


def _settlement_error(exc: Exception) -> HTTPException:
    """4xx, and never an oracle: a duplicate never names who owns the original."""
    code = str(exc)
    if code in ("INVOICE_NOT_FOUND", "SETTLEMENT_NOT_FOUND"):
        return HTTPException(status.HTTP_404_NOT_FOUND, "NOT_FOUND")
    if code == "SETTLEMENT_ALREADY_RECORDED":
        return HTTPException(status.HTTP_409_CONFLICT, code)
    return HTTPException(status.HTTP_400_BAD_REQUEST, code)


@router.post("/invoices/{invoice_id}/settle", dependencies=[Depends(_ADMIN)])
async def settle_invoice(request: Request, invoice_id: str, body: SettleIn,
                         principal: Principal = Depends(_ADMIN)) -> dict:
    """Record a payment made outside any gateway, and settle the invoice with it."""
    svc = build_billing(request)
    tenant = None if "platform.billing" in principal.caps else (principal.tenant or "__none__")
    try:
        return await svc.settle(invoice_id=invoice_id, tenant=tenant, **body.model_dump())
    except _stl.SettlementError as e:
        raise _settlement_error(e) from e


@router.get("/invoices/{invoice_id}/settlements", dependencies=[Depends(_SELF)])
async def list_settlements(request: Request, invoice_id: str,
                           principal: Principal = Depends(_SELF)) -> dict:
    svc = build_billing(request)
    tenant = None if "platform.billing" in principal.caps else (principal.tenant or "__none__")
    try:
        rows = await svc.settlements_for(invoice_id, tenant=tenant)
    except _stl.SettlementError as e:
        raise _settlement_error(e) from e
    return {"settlements": rows, "count": len(rows)}


@router.post("/settlements/{settlement_id}/reverse", dependencies=[Depends(_ADMIN)])
async def reverse_settlement(request: Request, settlement_id: str, body: ReverseIn,
                             principal: Principal = Depends(_ADMIN)) -> dict:
    """A recalled or bounced transfer. History is never deleted; a credit note is issued."""
    # Write-ahead: a critical action the log cannot take must not happen at all, so this
    # runs BEFORE the mutation. Recording afterwards would 503 a caller whose action had
    # already succeeded, and leave it unrecorded.
    await request.app.state.auditor.record(
        "settlement.reversed", actor=principal.subject, tenant=principal.tenant, resource=f"settlement:{settlement_id}",
        ip=request.client.host if request.client else "")
    svc = build_billing(request)
    tenant = None if "platform.billing" in principal.caps else (principal.tenant or "__none__")
    try:
        return await svc.reverse_settlement(settlement_id=settlement_id, tenant=tenant,
                                            reason=body.reason, actor=body.actor)
    except _stl.SettlementError as e:
        raise _settlement_error(e) from e
