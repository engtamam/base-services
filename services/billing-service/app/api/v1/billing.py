# =============================================================================
# billing-service — admin (catalog/ops) + customer (subscribe/invoices) routes
# =============================================================================
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from pydantic import BaseModel, Field
from starlette.responses import Response

from app.api.auth import Principal, require_user
from app.api.authz import require_any_capability, require_capability
from app.api.ratelimit import rate_limit
from app.config import get_settings
from app.core import settlement as _stl
from app.core.events import Emitter
from app.core.grants import GrantOutbox
from app.core.billing_service import BillingService
from app.core.stores import (CouponStore, DiscountRuleStore, IdempotencyStore, InvoiceStore,
                             LedgerStore, PayLinkStore, PaymentStore, PlanStore, SettlementStore,
                             SubscriptionStore, UsageStore)
from app.core.usage_service import UsageService

router = APIRouter(prefix="/api/v1/billing", tags=["billing"])

_ADMIN = require_any_capability("platform.billing", "billing.manage")
# Cross-tenant reads are a PLATFORM privilege — billing.manage may be a tenant-scoped
# admin, and must never reach across tenants. platform.billing alone.
_PLATFORM = require_capability("platform.billing")
_SELF = require_any_capability("billing.self", "billing.manage", "platform.billing")


def build_grants(request: Request, s) -> GrantOutbox:
    """The grant outbox for this request.

    Always built, even with the null driver: the promise is recorded whether or not anything can
    deliver it yet, so turning a wallet on later delivers everything that accumulated rather than
    starting from empty.
    """
    from app.core.models import iso, now_ts
    return GrantOutbox(
        request.app.state.container.datastore,
        getattr(request.app.state, "wallet", None),
        clock=now_ts, iso=iso,
        cfg={"grant_fund": s.WALLET_GRANT_FUND,
             "grant_retry_base_seconds": s.GRANT_RETRY_BASE_SECONDS,
             "grant_retry_max_seconds": s.GRANT_RETRY_MAX_SECONDS,
             "clawback_allow_negative": s.BILLING_CLAWBACK_ALLOW_NEGATIVE},
        # Best-effort: emits grant.clawback_failed when a reversal is permanently refused. The
        # durable FAILED row is the truth regardless; the event just lets an operator surface react.
        emitter=Emitter(request.app.state.container.event_bus,
                        fail_closed=s.EVENT_PUBLISH_FAILURE == "fail_closed"))


def _make_gateway_resolver(request: Request, settings):
    """A callable (tenant, driver) -> a gateway built for THAT driver with the tenant's credentials.

    Used only in pinned mode, to charge a subscription's own gateway. Fail-closed per driver (an
    unbuildable one comes back as an _UnconfiguredGateway whose charge raises), so a pinned renewal
    never lands on a different gateway than the one that issued its token."""
    async def _resolve(tenant: str, driver: str):
        from app.container import build_named_gateway
        r = getattr(request.app.state, "credentials", None)
        resolved = await r.resolve(tenant) if r is not None else {}
        return build_named_gateway(settings, driver, resolved)
    return _resolve


async def build_billing(request: Request, tenant: str = "") -> BillingService:
    """Assemble the service for this request.

    Async because the payment gateway is built from credentials an operator can change after
    launch, rather than captured once at boot. The resolver caches per tenant, so this is a dict
    lookup on all but the first call in each refresh window — see `core/credentials.py`.
    """
    c = request.app.state.container
    s = get_settings()
    gateway = c.payment_gateway
    resolver = getattr(request.app.state, "credentials", None)
    if resolver is not None:
        from app.container import rebuild_gateway
        # The tenant's runtime provider choice (operator-settable via /operator/config), or the boot
        # driver. Read at the point of use — the promise RUNTIME_POLICY makes — and folded into the
        # gateway cache key so a switch takes effect on the next charge without a redeploy.
        driver = s.PAYMENT_GATEWAY_DRIVER
        rc = getattr(request.app.state, "runtime_config", None)
        if rc is not None:
            try:
                driver = str(await rc.get(tenant=tenant, key="PAYMENT_GATEWAY_DRIVER",
                                          default=driver)) or driver
            except Exception:  # noqa: BLE001 — a config read must never break a charge; fall to boot driver
                driver = s.PAYMENT_GATEWAY_DRIVER
        gateway = await resolver.gateway(
            tenant, lambda resolved, boot: rebuild_gateway(s, resolved, boot, driver=driver),
            c.payment_gateway, cache_key_extra=driver)
    # Auto-reactivate policy — per tenant, runtime-editable, read at the point of use.
    auto_reactivate = bool(s.BILLING_AUTO_REACTIVATE)
    rc = getattr(request.app.state, "runtime_config", None)
    if rc is not None:
        try:
            v = await rc.get(tenant=tenant, key="BILLING_AUTO_REACTIVATE",
                             default=auto_reactivate)
            auto_reactivate = (v.strip().lower() not in ("false", "0", "no", "off", "")
                               if isinstance(v, str) else bool(v))
        except Exception:  # noqa: BLE001 — a config read must never break a charge
            auto_reactivate = bool(s.BILLING_AUTO_REACTIVATE)
    # Grant clawback master switch — per tenant, runtime-editable, read at the point of use.
    clawback_enabled = bool(s.BILLING_CLAWBACK_ENABLED)
    if rc is not None:
        try:
            v = await rc.get(tenant=tenant, key="BILLING_CLAWBACK_ENABLED", default=clawback_enabled)
            clawback_enabled = (v.strip().lower() not in ("false", "0", "no", "off", "")
                                if isinstance(v, str) else bool(v))
        except Exception:  # noqa: BLE001 — a config read must never break a refund
            clawback_enabled = bool(s.BILLING_CLAWBACK_ENABLED)
    # Which money document a paid invoice produces — per tenant, runtime-editable, read at use.
    document_mode = s.BILLING_DOCUMENT_MODE
    if rc is not None:
        try:
            v = await rc.get(tenant=tenant, key="BILLING_DOCUMENT_MODE", default=document_mode)
            v = str(v).strip().lower()
            document_mode = v if v in ("receipt", "tax_invoice", "both") else s.BILLING_DOCUMENT_MODE
        except Exception:  # noqa: BLE001 — a config read must never break a charge
            document_mode = s.BILLING_DOCUMENT_MODE

    # The REST of the tax profile, read AS ONE UNIT with the mode — the whole point is that a tax
    # invoice's completeness (calculator + rate + seller number) is decided from live values at the
    # point of use, never a boot snapshot half of which needed a redeploy. A read must never break a
    # charge, so each falls back to its configured value.
    async def _rc_str(key: str, default: str) -> str:
        if rc is None:
            return default
        try:
            v = await rc.get(tenant=tenant, key=key, default=default)
            return str(v) if v is not None else default
        except Exception:  # noqa: BLE001 — a config read must never break a charge
            return default
    async def _rc_int(key: str, default: int) -> int:
        # A GLOBAL knob (platform scope, tenant=""), not per-tenant — the included-allowance percent is
        # a platform decision. A read must never break a charge, so any failure falls back to config.
        if rc is None:
            return int(default)
        try:
            v = await rc.get(tenant="", key=key, default=default)
            return int(v)
        except Exception:  # noqa: BLE001 — a config read must never break a charge
            return int(default)
    tax_driver = await _rc_str("TAX_CALCULATOR_DRIVER", s.TAX_CALCULATOR_DRIVER)
    vat_rate = await _rc_str("VAT_RATE", s.VAT_RATE)
    einvoicer_driver = await _rc_str("EINVOICER_DRIVER", s.EINVOICER_DRIVER)
    # The seller identity is the PLATFORM's — its registration serves every tenant. Read at the
    # effective scope (tenant override, then the platform value) so a reseller with its own
    # registration MAY override, while completeness is evaluated against whatever actually applies.
    seller = {"name": await _rc_str("INVOICE_SELLER_NAME", s.INVOICE_SELLER_NAME),
              "vat_number": await _rc_str("INVOICE_SELLER_VAT", s.INVOICE_SELLER_VAT),
              "cr_number": await _rc_str("INVOICE_SELLER_CR", s.INVOICE_SELLER_CR),
              "address": await _rc_str("INVOICE_SELLER_ADDRESS", s.INVOICE_SELLER_ADDRESS)}
    return BillingService(
        plans=PlanStore(c.datastore, c.cache), coupons=CouponStore(c.datastore),
        discount_rules=DiscountRuleStore(c.datastore),
        subs=SubscriptionStore(c.datastore, c.cache),
        invoices=InvoiceStore(c.datastore), payments=PaymentStore(c.datastore), usage=UsageStore(c.datastore),
        paylinks=PayLinkStore(c.datastore),
        ledger=LedgerStore(c.datastore, cas_max_retries=s.LEDGER_CAS_MAX_RETRIES,
                           cas_backoff_ms=s.LEDGER_CAS_BACKOFF_MS),
        idempo=IdempotencyStore(c.datastore),
        emitter=Emitter(c.event_bus,
                        fail_closed=s.EVENT_PUBLISH_FAILURE == "fail_closed"),
        grants=build_grants(request, s),
        gateway=gateway, tax=c.tax_calculator, einvoicer=c.einvoicer, pdf=c.pdf_renderer,
        # pinned-mode: resolve a subscription's OWN gateway by (tenant, driver) at charge time, with
        # that tenant's stored credentials, fail-closed. None-safe (active mode never calls it).
        gateway_resolver=_make_gateway_resolver(request, s),
        # The wallet used for RENEWAL-BY-DEBIT (same instance the grant outbox delivers through).
        wallet=getattr(request.app.state, "wallet", None),
        cache=c.cache, settlements=SettlementStore(c.datastore),
        config={"currency": s.CURRENCY, "trial_days": s.TRIAL_DAYS, "vat_rate": vat_rate,
                # The tax profile, resolved together — completeness (tax_profile.py) is judged from
                # these at issue AND at render, and the seller snapshot is frozen onto a tax invoice.
                "tax_driver": tax_driver, "einvoicer_driver": einvoicer_driver, "seller": seller,
                "reverse_charge": s.REVERSE_CHARGE_B2B, "grandfather": s.GRANDFATHER,
                "dunning_base_hours": s.DUNNING_RETRY_BASE_HOURS, "dunning_max_attempts": s.DUNNING_MAX_ATTEMPTS,
                "grace_days": s.GRACE_DAYS, "list_limit": s.BILLING_LIST_LIMIT,
                "platform_max_pages": s.PLATFORM_MAX_PAGES,
                "cycle_sweep_limit": s.CYCLE_SWEEP_LIMIT,
                "cycle_lead_seconds": s.CYCLE_LEAD_SECONDS,
                "renewal_invoice_mode": s.BILLING_RENEWAL_INVOICE_MODE,
                "grant_fund": s.WALLET_GRANT_FUND,
                "plan_credit_limit_key": s.PLAN_CREDIT_LIMIT_KEY,
                "plan_credit_grant_mode": s.PLAN_CREDIT_GRANT_MODE,
                # Global (platform-scope) runtime knob: the included-allowance percent for opted-in
                # plans. Read live here so a panel change takes effect next cycle with no redeploy.
                "plan_credit_percent": await _rc_int("PLAN_CREDIT_PERCENT", s.PLAN_CREDIT_PERCENT),
                # Refund/chargeback grant clawback — a set of operator choices, all read at use.
                "clawback_enabled": clawback_enabled,
                "document_mode": document_mode,
                "clawback_allow_negative": s.BILLING_CLAWBACK_ALLOW_NEGATIVE,
                "clawback_on_partial": s.BILLING_CLAWBACK_ON_PARTIAL,
                "clawback_skip_expired": s.BILLING_CLAWBACK_SKIP_EXPIRED,
                "auto_reactivate": auto_reactivate,
                # How a renewal picks its gateway (active | pinned) — deployment-level, so a platform
                # chooses one behaviour. Read from settings, not per-tenant: the mode is the platform's.
                "renewal_gateway_mode": s.PAYMENT_RENEWAL_GATEWAY_MODE,
                # Renewal by wallet debit (for gateways with no recurring, e.g. TuwaiqPay). Off by
                # default; the platform names the fund to draw from — billing never guesses one.
                "wallet_renewal_enabled": s.BILLING_WALLET_RENEWAL_ENABLED,
                "wallet_renewal_fund": s.WALLET_RENEWAL_FUND,
                # One-off wallet top-up (buy prepaid credit). Fund + tax treatment + a fat-finger cap,
                # all platform-owned config — never hardcoded here.
                "wallet_topup_fund": s.WALLET_TOPUP_FUND,
                "wallet_topup_tax_mode": s.WALLET_TOPUP_TAX_MODE,
                "wallet_topup_min_minor": s.WALLET_TOPUP_MIN_MINOR,
                "wallet_topup_max_minor": s.WALLET_TOPUP_MAX_MINOR,
                # Pay-by-link: how long a generated invoice pay-link stays valid (default 72h).
                "pay_link_ttl_seconds": s.BILLING_PAY_LINK_TTL_SECONDS,
                # Entitlement resolution (Phase 42). Required by EntitlementResolver —
                # every default lives in config.py, never inline at a call site.
                "billable_statuses": s.billable_statuses,
                "max_subscriptions": s.MAX_BILLABLE_SUBSCRIPTIONS,
                "merge_strategies": s.entitlement_merge_strategies,
                "rank_orders": s.entitlement_rank_orders,
                "period_tolerance_seconds": s.ENTITLEMENT_PERIOD_TOLERANCE_SECONDS,
                "limits_cache_ttl": s.LIMITS_CACHE_TTL_SECONDS,
                "settlement_methods": s.settlement_methods,
                "settlement_allow_partial": s.SETTLEMENT_ALLOW_PARTIAL,
                "settlement_tolerance_minor": s.SETTLEMENT_TOLERANCE_MINOR,
                "settlement_max_backdate_days": s.SETTLEMENT_MAX_BACKDATE_DAYS})


async def build_usage(request: Request) -> UsageService:
    """UsageService owns the usage number. Routes never accept one from the caller."""
    c = request.app.state.container
    s = get_settings()
    billing = (await build_billing(request))
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
    credit_percent_of_price: bool = False   # opt in to a percent-of-price included allowance


class PlanUpdateIn(BaseModel):
    """Every field optional: an operator correcting a price must not have to resend the limits and
    risk clobbering them with whatever the form happened to hold."""

    name: str | None = Field(default=None, min_length=1, max_length=128)
    prices: list[dict[str, Any]] | None = Field(default=None, min_length=1)
    features: list[str] | None = None
    limits: dict[str, Any] | None = None
    trial_days: int | None = Field(default=None, ge=0)
    credit_percent_of_price: bool | None = None


class PlanAvailabilityIn(BaseModel):
    active: bool | None = None      # False: nobody NEW may subscribe; existing subs are untouched
    listed: bool | None = None      # False: hidden from the plan list, still subscribable


class CouponIn(BaseModel):
    """Everything a coupon actually has.

    The previous model carried four of these fields, so `expires_at` and `active` could not be set
    through the API at all and reset to their defaults on every write — a panel offering "expires
    on" or "turn off" was offering something the service could not do.
    """

    code: str = Field(min_length=1, max_length=64)
    type: str = Field(default="percent", pattern="^(percent|fixed|trial)$")
    value: int = Field(default=0, ge=0)
    currency: str = Field(default="SAR", max_length=8)
    starts_at: float | None = None
    expires_at: float | None = None
    max_redemptions: int = Field(default=0, ge=0)     # 0 = unlimited
    plan_id: str = Field(default="", max_length=128)  # scope to ONE plan; empty = any plan
    active: bool = True
    public: bool = False                              # show to customers on the offers endpoint
    note: str = Field(default="", max_length=512)


class CouponPatchIn(BaseModel):
    """A partial edit. Every field optional, and `redeemed` is not among them: the counter belongs
    to what customers did, not to what an operator is changing."""

    type: str | None = Field(default=None, pattern="^(percent|fixed|trial)$")
    value: int | None = Field(default=None, ge=0)
    currency: str | None = Field(default=None, max_length=8)
    starts_at: float | None = None
    expires_at: float | None = None
    max_redemptions: int | None = Field(default=None, ge=0)
    plan_id: str | None = Field(default=None, max_length=128)   # scope to ONE plan; "" = any plan
    active: bool | None = None
    public: bool | None = None
    note: str | None = Field(default=None, max_length=512)


class DiscountRuleIn(BaseModel):
    """A cross-app discount rule: holding `owning_key` grants `percent`% off any subscription whose
    plan carries `target_key`. No code, no redemption count — it applies automatically while owned."""

    owning_key: str = Field(min_length=1, max_length=128)
    target_key: str = Field(min_length=1, max_length=128)
    percent: int = Field(ge=0, le=100)                 # 100 = fully free, same reach as a coupon
    active: bool = True
    starts_at: float | None = None
    expires_at: float | None = None
    note: str = Field(default="", max_length=512)


class DiscountRulePatchIn(BaseModel):
    """A partial edit; every field optional. id/created are never editable."""

    owning_key: str | None = Field(default=None, max_length=128)
    target_key: str | None = Field(default=None, max_length=128)
    percent: int | None = Field(default=None, ge=0, le=100)
    active: bool | None = None
    starts_at: float | None = None
    expires_at: float | None = None
    note: str | None = Field(default=None, max_length=512)


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
@router.post("/plans", status_code=201)
async def create_plan(request: Request, body: PlanIn,
                      principal: Principal = Depends(_ADMIN)) -> dict:
    p = await (await build_billing(request)).create_plan(**body.model_dump())
    await request.app.state.auditor.record(
        "plan.created", actor=principal.subject, tenant=principal.tenant,
        resource=f"plan:{p.id}", ip=request.client.host if request.client else "")
    return p.to_dict()


@router.get("/offers", dependencies=[Depends(rate_limit)])
async def public_offers(request: Request) -> dict:
    """Public discount campaigns, for a customer app — NO login required.

    The customer could not learn a campaign existed: list_coupons is operator-only. This is the
    single source of truth an app reads and auto-applies, so a discount is not mirrored into a
    second store that drifts. Shows only coupons marked public AND currently usable, and only
    customer-safe fields — never max_redemptions, redeemed or the operator note.
    """
    return {"offers": await (await build_billing(request)).public_offers()}


@router.get("/public/plans", dependencies=[Depends(rate_limit)])
async def public_plans(request: Request) -> dict:
    """The published catalogue, for a pricing page — NO capability required.

    This was the missing line. Every other plan route needs a capability, which is right for the
    panel and useless for the surface that most needs prices: a visitor deciding whether to sign
    up has no principal, so the pricing page was the one page that could not read the prices.

    UNAUTHENTICATED, so three things are deliberate. It shows only plans the operator marked
    `listed` AND `active`, because those switches mean nothing if one route ignores them. It
    returns a WHITELIST of fields rather than the stored row, so a field added to Plan later
    cannot leak by default. And it is rate limited like everything else, because an endpoint
    anyone can call is an endpoint anyone can hammer.

    Deliberately NOT here: coupons. A discount code that anybody can enumerate is a discount
    everybody has.
    """
    return {"plans": await (await build_billing(request)).public_plans()}


@router.get("/plans", dependencies=[Depends(_SELF)])
async def list_plans(request: Request, include_hidden: bool = False,
                     principal: Principal = Depends(require_user)) -> dict:
    # `include_hidden` is an ADMIN view. Honouring it for any caller would turn the public plan
    # list into a way to enumerate retired and unlisted pricing.
    if include_hidden:
        await _ADMIN(principal)
    return {"plans": await (await build_billing(request)).list_plans(include_hidden=include_hidden)}


@router.get("/discount-preview", dependencies=[Depends(_SELF)])
async def discount_preview(request: Request, principal: Principal = Depends(require_user)) -> dict:
    """Per-plan "you already qualify for X% off" for the CALLING shop — a browse-time preview of the
    cross-app discount rules, so an owner discovers the cross-sell before checkout. Same auth as the
    plan list (any signed-in shop user), ownership-only (no coupon), charges/creates nothing.

    -> {"previews": {plan_id: {"percent": int, "owning_key": str}}} — a plan with no applicable rule
    is absent. `owning_key` names WHICH app of theirs earned it, so the badge can say why."""
    tenant = principal.tenant or "__none__"
    return {"previews": await (await build_billing(request)).discount_preview(tenant)}


@router.patch("/plans/{plan_id}", dependencies=[Depends(_PLATFORM)])
async def update_plan(request: Request, plan_id: str, body: PlanUpdateIn,
                      principal: Principal = Depends(require_user)) -> dict:
    """Correct a live plan. Existing subscribers keep the terms they were sold."""
    await request.app.state.auditor.record(
        "plan.updated", actor=principal.subject, tenant=principal.tenant,
        resource=f"plan:{plan_id}", ip=request.client.host if request.client else "",
        metadata={"fields": sorted(k for k, v in body.model_dump().items() if v is not None)})
    try:
        plan = await (await build_billing(request)).update_plan(
            plan_id, **{k: v for k, v in body.model_dump().items() if v is not None})
    except ValueError as exc:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, str(exc)) from exc
    return plan.to_dict()


@router.post("/plans/{plan_id}/availability", dependencies=[Depends(_PLATFORM)])
async def plan_availability(request: Request, plan_id: str, body: PlanAvailabilityIn,
                            principal: Principal = Depends(require_user)) -> dict:
    """Retire or hide a plan. Nobody already on it is affected."""
    await request.app.state.auditor.record(
        "plan.availability_changed", actor=principal.subject, tenant=principal.tenant,
        resource=f"plan:{plan_id}", ip=request.client.host if request.client else "",
        metadata={"active": body.active, "listed": body.listed})
    try:
        plan = await (await build_billing(request)).set_plan_availability(
            plan_id, active=body.active, listed=body.listed)
    except ValueError as exc:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, str(exc)) from exc
    return plan.to_dict()


@router.post("/coupons", status_code=201, dependencies=[Depends(_ADMIN)])
async def create_coupon(request: Request, body: CouponIn,
                        principal: Principal = Depends(require_user)) -> dict:
    await request.app.state.auditor.record(
        "coupon.created", actor=principal.subject, tenant=principal.tenant,
        resource=f"coupon:{body.code}", ip=request.client.host if request.client else "")
    return (await (await build_billing(request)).upsert_coupon(**body.model_dump())).to_dict()


@router.get("/coupons", dependencies=[Depends(_ADMIN)])
async def list_coupons(request: Request,
                       status_: str = Query(default="", alias="status", max_length=16)) -> dict:
    """Every coupon, with why each is or is not discounting.

    There was no route at all: an operator could create a code and then had to remember it, for
    ever, because nothing could tell them what existed.
    """
    try:
        return {"coupons": await (await build_billing(request)).list_coupons(status=status_)}
    except ValueError as exc:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, str(exc)) from exc


@router.get("/coupons/{code}", dependencies=[Depends(_ADMIN)])
async def get_coupon(request: Request, code: str) -> dict:
    row = await (await build_billing(request)).get_coupon(code)
    if row is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "NOT_FOUND")
    return row


@router.patch("/coupons/{code}", dependencies=[Depends(_ADMIN)])
async def update_coupon(request: Request, code: str, body: CouponPatchIn,
                        principal: Principal = Depends(require_user)) -> dict:
    """Edit a coupon without resetting what customers have already redeemed."""
    await request.app.state.auditor.record(
        "coupon.updated", actor=principal.subject, tenant=principal.tenant,
        resource=f"coupon:{code}", ip=request.client.host if request.client else "",
        metadata={"fields": sorted(k for k, v in body.model_dump().items() if v is not None)})
    try:
        return await (await build_billing(request)).update_coupon(
            code, **{k: v for k, v in body.model_dump().items() if v is not None})
    except ValueError as exc:
        code_ = status.HTTP_404_NOT_FOUND if "NOT_FOUND" in str(exc) else status.HTTP_400_BAD_REQUEST
        raise HTTPException(code_, str(exc)) from exc


@router.delete("/coupons/{code}", dependencies=[Depends(_ADMIN)])
async def delete_coupon(request: Request, code: str,
                        principal: Principal = Depends(require_user)) -> dict:
    """Remove a coupon outright.

    Distinct from turning one off: a mistyped code nobody used should disappear, while a campaign
    customers redeemed should be deactivated and KEPT, so the record of what was honoured survives.
    """
    await request.app.state.auditor.record(
        "coupon.deleted", actor=principal.subject, tenant=principal.tenant,
        resource=f"coupon:{code}", ip=request.client.host if request.client else "")
    return {"deleted": await (await build_billing(request)).delete_coupon(code)}


# ── cross-app discount rules (operator) ─────────────────────────────────────────
@router.post("/discount-rules", status_code=201, dependencies=[Depends(_ADMIN)])
async def create_discount_rule(request: Request, body: DiscountRuleIn,
                               principal: Principal = Depends(require_user)) -> dict:
    await request.app.state.auditor.record(
        "discount_rule.created", actor=principal.subject, tenant=principal.tenant,
        resource=f"discount_rule:{body.owning_key}->{body.target_key}",
        ip=request.client.host if request.client else "")
    try:
        return await (await build_billing(request)).create_discount_rule(**body.model_dump())
    except ValueError as exc:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, str(exc)) from exc


@router.get("/discount-rules", dependencies=[Depends(_ADMIN)])
async def list_discount_rules(request: Request) -> dict:
    try:
        return {"discount_rules": await (await build_billing(request)).list_discount_rules()}
    except ValueError as exc:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, str(exc)) from exc


@router.patch("/discount-rules/{rule_id}", dependencies=[Depends(_ADMIN)])
async def update_discount_rule(request: Request, rule_id: str, body: DiscountRulePatchIn,
                               principal: Principal = Depends(require_user)) -> dict:
    await request.app.state.auditor.record(
        "discount_rule.updated", actor=principal.subject, tenant=principal.tenant,
        resource=f"discount_rule:{rule_id}", ip=request.client.host if request.client else "",
        metadata={"fields": sorted(k for k, v in body.model_dump().items() if v is not None)})
    try:
        return await (await build_billing(request)).update_discount_rule(
            rule_id, **{k: v for k, v in body.model_dump().items() if v is not None})
    except ValueError as exc:
        code_ = status.HTTP_404_NOT_FOUND if "NOT_FOUND" in str(exc) else status.HTTP_400_BAD_REQUEST
        raise HTTPException(code_, str(exc)) from exc


@router.delete("/discount-rules/{rule_id}", dependencies=[Depends(_ADMIN)])
async def delete_discount_rule(request: Request, rule_id: str,
                               principal: Principal = Depends(require_user)) -> dict:
    await request.app.state.auditor.record(
        "discount_rule.deleted", actor=principal.subject, tenant=principal.tenant,
        resource=f"discount_rule:{rule_id}", ip=request.client.host if request.client else "")
    return {"deleted": await (await build_billing(request)).delete_discount_rule(rule_id)}


# ── subscriptions (customer) ────────────────────────────────────────────────────
@router.post("/subscriptions", status_code=201)
async def subscribe(request: Request, body: SubscribeIn, principal: Principal = Depends(_SELF)) -> dict:
    try:
        sub = await (await build_billing(request)).subscribe(
            tenant=_tenant(principal, body.tenant), plan_id=body.plan_id, interval=body.interval,
            quantity=body.quantity, coupon_code=body.coupon_code, payment_token=body.payment_token,
            buyer_name=body.buyer_name, buyer_vat=body.buyer_vat, buyer_cr=body.buyer_cr)
    except ValueError as e:
        raise HTTPException(status.HTTP_404_NOT_FOUND, str(e))
    await request.app.state.auditor.record(
        "subscription.created", actor=principal.subject, tenant=sub.tenant,
        resource=f"subscription:{sub.id}", ip=request.client.host if request.client else "",
        metadata={"plan": body.plan_id})
    return sub.to_dict()


class CheckoutIn(BaseModel):
    amount_minor: int = Field(gt=0)
    currency: str = Field(default="", max_length=8)
    reference: str = Field(default="", max_length=128)
    return_url: str = Field(default="", max_length=1024)
    tenant: str | None = None


@router.post("/checkouts", status_code=201, dependencies=[Depends(rate_limit)])
async def create_checkout(request: Request, body: CheckoutIn,
                          principal: Principal = Depends(_SELF)) -> dict:
    """Server-create a payment checkout/session for the customer's widget or redirect.

    We never proxy a provider key: the client gets the session/checkout id (and, for a widget, the
    provider's own script URL), never our credentials. A provider with no server-side session says
    so (`supported: false`). Completion reconciles through the existing webhook / GET-status path
    into _mark_paid, so a completed checkout is the SAME paid invoice as a direct charge.
    """
    from app.ports.payment_gateway import GatewayUnavailable
    tenant = _tenant(principal, body.tenant)
    try:
        return await (await build_billing(request, tenant)).prepare_checkout(
            amount_minor=body.amount_minor, currency=body.currency, reference=body.reference,
            return_url=body.return_url)
    except GatewayUnavailable as exc:
        # Unreachable / unconfigured gateway — retryable, not a client error.
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, str(exc)) from exc


class PayLinkIn(BaseModel):
    # A platform operator names the tenant whose invoice to bill; a tenant admin's value is ignored
    # (scoped to their own tenant by _tenant()) — the operator-tenant-wired pattern.
    tenant: str | None = None
    return_url: str = Field(default="", max_length=1024)
    ttl_seconds: int | None = Field(default=None, ge=0)


@router.post("/invoices/{invoice_id}/pay-link", status_code=201, dependencies=[Depends(rate_limit)])
async def create_invoice_pay_link(request: Request, invoice_id: str, body: PayLinkIn,
                                  principal: Principal = Depends(_SELF)) -> dict:
    """A trackable hosted-checkout LINK for an existing unpaid invoice — to send to the payer (e.g.
    over WhatsApp). Amount = the invoice total (server-authoritative). Completion flows through the
    same webhook/reconcile -> _mark_paid seam, marking THIS invoice paid once. Returns `checkout_url`
    (fully dynamic — use inside the 24h window as a cta_url) and `pay_url` (a stable redirector link,
    when BILLING_PAY_LINK_BASE_URL is set, so it can ride inside a Meta template's URL button)."""
    from app.core.billing_service import UpgradeChargeFailed, UpgradeChargeUnavailable
    s = get_settings()
    tenant = _tenant(principal, body.tenant)
    try:
        res = await (await build_billing(request, tenant)).create_invoice_pay_link(
            tenant=tenant, invoice_id=invoice_id, return_url=body.return_url,
            ttl_seconds=int(body.ttl_seconds or 0))
    except ValueError as exc:
        msg = str(exc)
        if msg == "INVOICE_NOT_FOUND":
            raise HTTPException(status.HTTP_404_NOT_FOUND, "NOT_FOUND") from exc
        if msg.startswith("INVOICE_NOT_PAYABLE"):
            raise HTTPException(status.HTTP_409_CONFLICT, msg) from exc
        raise HTTPException(status.HTTP_400_BAD_REQUEST, msg) from exc
    except UpgradeChargeUnavailable as exc:
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, "GATEWAY_UNAVAILABLE") from exc
    except UpgradeChargeFailed as exc:
        raise HTTPException(status.HTTP_502_BAD_GATEWAY, str(exc) or "CHECKOUT_FAILED") from exc
    await request.app.state.auditor.record(
        "billing.pay_link_created", actor=principal.subject, tenant=tenant,
        resource=f"invoice:{invoice_id}", ip=request.client.host if request.client else "",
        metadata={"payment_id": res["payment_id"]})
    base = (s.BILLING_PAY_LINK_BASE_URL or "").strip()
    pay_url = (base.rstrip("/") + "/" + res["token"]) if (base and res.get("token")) else res["checkout_url"]
    return {"payment_id": res["payment_id"], "invoice_id": res["invoice_id"],
            "amount_minor": res["amount_minor"], "currency": res["currency"],
            "checkout_url": res["checkout_url"], "pay_url": pay_url, "expires_at": res["expires_at"]}


@router.get("/pay/{token}", dependencies=[Depends(rate_limit)])
async def pay_link_redirect(request: Request, token: str):
    """PUBLIC redirector: 302 a pay-link token to its gateway hosted-checkout page. No auth (the
    unguessable token IS the capability, meant to be shared with the payer); no data leaked (just a
    redirect). The target URL was minted by us from the gateway session (never caller input); even so,
    it is defended: https-only and an optional host allow-list."""
    from urllib.parse import urlsplit

    from starlette.responses import RedirectResponse
    s = get_settings()
    res = await (await build_billing(request, "")).resolve_pay_link(token)
    if res is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "NOT_FOUND")
    if res.get("expired"):
        raise HTTPException(status.HTTP_410_GONE, "LINK_EXPIRED")
    if res.get("settled"):
        # The invoice is no longer open (paid/void) — the link is dead, so a payer cannot pay an
        # already-settled invoice through a still-pending link.
        raise HTTPException(status.HTTP_410_GONE, "INVOICE_SETTLED")
    url = res.get("checkout_url", "")
    parts = urlsplit(url)
    if parts.scheme != "https" or not parts.hostname:
        raise HTTPException(status.HTTP_502_BAD_GATEWAY, "BAD_CHECKOUT_URL")
    # FAIL CLOSED against open-redirect: the redirector must not 302 to an unvetted host. The
    # allow-list is REQUIRED to use the redirector — a tenant with a custom/base-overridden gateway
    # could otherwise steer checkout_url to an attacker host under the platform's domain (phishing).
    allowed = [h.strip() for h in (s.BILLING_PAY_LINK_ALLOWED_HOSTS or "").split(",") if h.strip()]
    if not allowed:
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE,
                            "PAY_LINK_ALLOWED_HOSTS not configured — the redirector is disabled until "
                            "the platform sets the gateway host allow-list")
    if parts.hostname not in allowed:
        raise HTTPException(status.HTTP_502_BAD_GATEWAY, "CHECKOUT_HOST_NOT_ALLOWED")
    return RedirectResponse(url, status_code=302)


def _check_topup_amount(amount_minor: int) -> None:
    """Enforce the configured min/max on a wallet top-up. Field(gt=0) only stops non-positive; the
    real bounds are config (WALLET_TOPUP_MIN/MAX_MINOR) so a platform tunes them per gateway/AML.
    Each bound with value 0 is disabled. A violation is a clean 400 — nothing is charged."""
    s = get_settings()
    lo, hi = int(s.WALLET_TOPUP_MIN_MINOR), int(s.WALLET_TOPUP_MAX_MINOR)
    if lo > 0 and amount_minor < lo:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, f"AMOUNT_BELOW_MIN:{lo}")
    if hi > 0 and amount_minor > hi:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, f"AMOUNT_ABOVE_MAX:{hi}")


class WalletTopupIn(BaseModel):
    amount_minor: int = Field(gt=0)
    payment_token: str = Field(min_length=1, max_length=512)
    fund: str = Field(default="", max_length=64)          # "" → the configured default fund
    currency: str = Field(default="", max_length=8)
    tenant: str | None = None


@router.post("/wallet-topups", status_code=201, dependencies=[Depends(rate_limit)])
async def wallet_topup(request: Request, body: WalletTopupIn,
                       principal: Principal = Depends(_SELF)) -> dict:
    """Charge a one-off amount and credit it to a wallet fund — a manual prepaid top-up (e.g. AI-usage
    credit). Reuses the paid-invoice machinery: the wallet is credited through the same durable,
    idempotent grant outbox a plan renewal uses (a redelivered webhook / a reconcile credits once; a
    refund claws back through the same seam). No VAT at top-up by default (payment on account)."""
    from app.core.billing_service import UpgradeChargeFailed, UpgradeChargeUnavailable
    _check_topup_amount(body.amount_minor)
    tenant = _tenant(principal, body.tenant)
    try:
        result = await (await build_billing(request, tenant)).wallet_topup(
            tenant=tenant, amount_minor=body.amount_minor, payment_token=body.payment_token,
            fund=body.fund, currency=body.currency)
    except UpgradeChargeUnavailable as exc:
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, "GATEWAY_UNAVAILABLE") from exc
    except UpgradeChargeFailed as exc:
        raise HTTPException(status.HTTP_402_PAYMENT_REQUIRED, str(exc) or "DECLINED") from exc
    except ValueError as exc:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, str(exc)) from exc
    await request.app.state.auditor.record(
        "billing.wallet_topped_up", actor=principal.subject, tenant=tenant,
        resource=f"invoice:{result['invoice_id']}", ip=request.client.host if request.client else "",
        metadata={"fund": result["fund"], "amount_minor": result["amount_minor"],
                  "currency": result["currency"]})
    return result


class WalletTopupCheckoutIn(BaseModel):
    amount_minor: int = Field(gt=0)
    fund: str = Field(default="", max_length=64)
    currency: str = Field(default="", max_length=8)
    return_url: str = Field(default="", max_length=1024)
    tenant: str | None = None


@router.post("/wallet-topups/checkout", status_code=201, dependencies=[Depends(rate_limit)])
async def wallet_topup_checkout(request: Request, body: WalletTopupCheckoutIn,
                                principal: Principal = Depends(_SELF)) -> dict:
    """Start a HOSTED-CHECKOUT wallet top-up — for a gateway with no card token (e.g. TuwaiqPay:
    bill → payment link → customer pays → poll/verify). Creates a PENDING invoice+payment carrying
    the top-up grant; completion (a webhook or POST /payments/{id}/reconcile) credits the wallet fund
    through the same _mark_paid seam as the token path. No VAT at top-up (payment on account)."""
    from app.core.billing_service import UpgradeChargeFailed, UpgradeChargeUnavailable
    _check_topup_amount(body.amount_minor)
    tenant = _tenant(principal, body.tenant)
    try:
        result = await (await build_billing(request, tenant)).wallet_topup_checkout(
            tenant=tenant, amount_minor=body.amount_minor, fund=body.fund,
            currency=body.currency, return_url=body.return_url)
    except UpgradeChargeUnavailable as exc:
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, "GATEWAY_UNAVAILABLE") from exc
    except UpgradeChargeFailed as exc:
        raise HTTPException(status.HTTP_502_BAD_GATEWAY, str(exc) or "CHECKOUT_FAILED") from exc
    except ValueError as exc:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, str(exc)) from exc
    await request.app.state.auditor.record(
        "billing.wallet_topup_started", actor=principal.subject, tenant=tenant,
        resource=f"invoice:{result['invoice_id']}", ip=request.client.host if request.client else "",
        metadata={"fund": result["fund"], "amount_minor": result["amount_minor"],
                  "currency": result["currency"]})
    return result


@router.post("/subscriptions/{sub_id}/change")
async def change_plan(request: Request, sub_id: str, body: ChangeIn,
                      principal: Principal = Depends(_SELF)) -> dict:
    svc = (await build_billing(request))
    sub = await svc._subs.fetch(sub_id)
    if sub is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "NOT_FOUND")
    _guard(principal, sub.tenant)
    from app.core.billing_service import UpgradeChargeFailed, UpgradeChargeUnavailable
    try:
        result = await svc.change_plan(sub_id, new_plan_id=body.new_plan_id,
                                       interval=body.interval, quantity=body.quantity)
    except UpgradeChargeFailed as e:
        # The upgrade's prorated charge was declined (or there is no card) — the plan did NOT change.
        # 402 so the shop can fix the payment method and retry; the reason is a domain code, not internals.
        raise HTTPException(status.HTTP_402_PAYMENT_REQUIRED, str(e))
    except UpgradeChargeUnavailable as e:
        # The gateway was unreachable — retryable, distinct from a decline.
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, str(e))
    except ValueError as e:
        raise HTTPException(status.HTTP_404_NOT_FOUND, str(e))
    await request.app.state.auditor.record(
        "subscription.changed", actor=principal.subject, tenant=sub.tenant,
        resource=f"subscription:{sub_id}", ip=request.client.host if request.client else "",
        metadata={"new_plan": body.new_plan_id or "", "applied": result.get("applied", "")})
    return result


@router.post("/subscriptions/{sub_id}/cancel-scheduled-change")
async def cancel_scheduled_change(request: Request, sub_id: str,
                                  principal: Principal = Depends(_SELF)) -> dict:
    """Undo a scheduled downgrade before it takes effect — the shop stays on its current plan.

    A no-op (cancelled_scheduled_change: false) when nothing was scheduled, so a double click or a
    stale button is harmless rather than an error."""
    svc = (await build_billing(request))
    sub = await svc._subs.fetch(sub_id)
    if sub is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "NOT_FOUND")
    _guard(principal, sub.tenant)
    result = await svc.cancel_scheduled_change(sub_id)
    await request.app.state.auditor.record(
        "subscription.scheduled_change_cancelled", actor=principal.subject, tenant=sub.tenant,
        resource=f"subscription:{sub_id}", ip=request.client.host if request.client else "")
    return result


@router.post("/subscriptions/{sub_id}/undo-cancel")
async def undo_cancel(request: Request, sub_id: str,
                      principal: Principal = Depends(_SELF)) -> dict:
    """Undo a scheduled cancellation on a still-active subscription — the "changed my mind" next to
    "won't renew after this period." reactivate() refuses an active subscription by design, so this is
    the dedicated undo. A no-op (undone_cancellation: false) when nothing is scheduled — harmless to
    double-click."""
    svc = (await build_billing(request))
    sub = await svc._subs.fetch(sub_id)
    if sub is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "NOT_FOUND")
    _guard(principal, sub.tenant)
    result = await svc.undo_cancel(sub_id)
    await request.app.state.auditor.record(
        "subscription.cancellation_undone", actor=principal.subject, tenant=sub.tenant,
        resource=f"subscription:{sub_id}", ip=request.client.host if request.client else "")
    return result


class PaymentMethodIn(BaseModel):
    payment_token: str = Field(min_length=1, max_length=512)


@router.post("/subscriptions/{sub_id}/payment-method")
async def update_payment_method(request: Request, sub_id: str, body: PaymentMethodIn,
                                principal: Principal = Depends(_SELF)) -> dict:
    """Replace the card on a subscription — self-service recovery from an expired/declined card.

    Without it, dunning retried the same failing token until the subscription was suspended: a tenant
    whose card merely expired had no path but a support ticket. This changes ONLY the stored token; the
    next scheduled dunning pass spends it, so nothing is charged here and no status moves. The token is
    the gateway's opaque handle for a card the tenant just re-tokenised on the console — never raw card
    data, which this platform never sees."""
    svc = (await build_billing(request))
    sub = await svc._subs.fetch(sub_id)
    if sub is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "NOT_FOUND")
    _guard(principal, sub.tenant)
    try:
        updated = await svc.update_payment_method(sub_id, body.payment_token)
    except ValueError as e:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, str(e))
    if updated is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "NOT_FOUND")
    await request.app.state.auditor.record(
        "subscription.payment_method_updated", actor=principal.subject, tenant=sub.tenant,
        resource=f"subscription:{sub_id}", ip=request.client.host if request.client else "")
    # The token is NEVER echoed back — a write-only credential, like the operator secret routes.
    return {"subscription_id": sub_id, "updated": True}


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
    svc = (await build_billing(request))
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
    svc = (await build_billing(request))
    sub = await svc._subs.fetch(sub_id)
    if sub is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "NOT_FOUND")
    _guard(principal, sub.tenant)
    result = (await svc.cancel(sub_id, at_period_end=at_period_end)).to_dict()
    await request.app.state.auditor.record(
        "subscription.cancelled", actor=principal.subject, tenant=sub.tenant,
        resource=f"subscription:{sub_id}", ip=request.client.host if request.client else "",
        metadata={"at_period_end": str(at_period_end)})
    return result


@router.post("/subscriptions/{sub_id}/run")
async def run_cycle(request: Request, sub_id: str, principal: Principal = Depends(_ADMIN)) -> dict:
    svc = await build_billing(request)
    sub = await svc._subs.fetch(sub_id)
    if sub is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "NOT_FOUND")
    _guard(principal, sub.tenant)                      # no forcing a charge on another tenant's sub
    try:
        return (await svc.run_cycle(sub_id)).to_dict()
    except ValueError as e:
        raise HTTPException(status.HTTP_404_NOT_FOUND, str(e))


def _page(limit: int | None, s) -> int:
    """A caller may ask for fewer rows, never more. An unbounded limit is a way to make the
    service read an entire collection on request."""
    ceiling = s.BILLING_LIST_LIMIT
    return max(1, min(int(limit or ceiling), ceiling))


def _scope(principal: Principal, tenant: str | None) -> str:
    """Which tenant this listing covers.

    `tenant` is honoured ONLY for a platform operator — _tenant() enforces that, and it is the
    single place the rule lives. For everyone else a cross-tenant request is refused outright
    rather than quietly answered with their own rows: silently substituting the caller's tenant
    would show them their own invoices under someone else's name, which is worse than an error.
    """
    if tenant and "platform.billing" not in principal.caps:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "CROSS_TENANT_FORBIDDEN")
    return _tenant(principal, tenant)


@router.get("/subscriptions", dependencies=[Depends(_SELF)])
async def list_subs(request: Request, status_: str | None = Query(default=None, alias="status",
                                                                  max_length=32),
                    plan_id: str | None = Query(default=None, max_length=64),
                    limit: int | None = Query(default=None, ge=1, le=1000),
                    cursor: str | None = Query(default=None, max_length=512),
                    tenant: str | None = Query(default=None, max_length=128),
                    principal: Principal = Depends(_SELF)) -> dict:
    """Subscriptions, filterable and paginated, so a UI can actually browse them."""
    s = get_settings()
    svc = await build_billing(request)
    try:
        rows, nxt = await svc._subs.browse(
            _scope(principal, tenant), limit=_page(limit, s), after=cursor,
            where={"status": status_, "plan_id": plan_id})
    except ValueError as exc:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, str(exc)) from exc
    return {"subscriptions": rows, "next_cursor": nxt}


@router.post("/usage", status_code=201)
async def record_usage(request: Request, body: UsageIn, principal: Principal = Depends(_SELF)) -> dict:
    tenant = _tenant(principal)
    result = await (await build_billing(request)).record_usage(
        tenant=tenant, subscription_id=body.subscription_id,
        metric=body.metric, quantity=body.quantity)
    await request.app.state.auditor.record(
        "usage.recorded", actor=principal.subject, tenant=tenant,
        resource=f"subscription:{body.subscription_id}",
        ip=request.client.host if request.client else "",
        metadata={"metric": body.metric, "quantity": str(body.quantity)})
    return result


# ── invoices ───────────────────────────────────────────────────────────────────
@router.get("/invoices", dependencies=[Depends(_SELF)])
async def list_invoices(request: Request, status_: str | None = Query(default=None, alias="status",
                                                                      max_length=32),
                        subscription_id: str | None = Query(default=None, max_length=64),
                        limit: int | None = Query(default=None, ge=1, le=1000),
                        cursor: str | None = Query(default=None, max_length=512),
                        tenant: str | None = Query(default=None, max_length=128),
                        principal: Principal = Depends(_SELF)) -> dict:
    """Invoices, filterable and paginated.

    Previously this returned one tenant's first N rows with no filter, no cursor and no way to
    tell that anything had been left out — so a finance page could not show unpaid invoices, could
    not reach the second page, and a platform operator could not see across tenants at all.
    """
    s = get_settings()
    svc = await build_billing(request)
    try:
        rows, nxt = await svc._invoices.browse(
            _scope(principal, tenant), limit=_page(limit, s), after=cursor,
            where={"status": status_, "subscription_id": subscription_id})
    except ValueError as exc:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, str(exc)) from exc
    return {"invoices": rows, "next_cursor": nxt}


@router.get("/admin/invoices", dependencies=[Depends(_PLATFORM), Depends(rate_limit)])
async def list_invoices_platform(request: Request,
                                 status_: str = Query(default="", alias="status", max_length=32),
                                 subscription_id: str = Query(default="", max_length=64),
                                 tenant: str = Query(default="", max_length=128),
                                 since: str = Query(default="", max_length=32),
                                 until: str = Query(default="", max_length=32),
                                 limit: int | None = Query(default=None, ge=1, le=1000),
                                 cursor: str | None = Query(default=None, max_length=512),
                                 principal: Principal = Depends(require_user)) -> dict:
    """Invoices ACROSS ALL TENANTS — a platform finance view. platform.billing only.

    Six things make this safe to cross the tenancy wall: the capability is platform-only (a
    tenant admin gets 403, never their own rows silently); a filter is mandatory (there is no
    unbounded ledger dump); it is paginated and capped (a stolen token cannot drain it in one
    call); it is audited write-ahead (who looked across every tenant, and when); it returns a
    money projection, not buyer PII; and it is a DISTINCT route, so it reads as the privileged one
    rather than a magic parameter on the tenant-scoped list.
    """
    if not (status_ or subscription_id or tenant or since or until):
        # A filter is mandatory — an unfiltered cross-tenant ledger dump is an exfiltration
        # primitive, not a feature. Any of status/tenant/subscription/date-window counts.
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "PLATFORM_LISTING_NEEDS_A_FILTER")
    # `since`/`until` bound an ISO-8601 issue date and are compared AS TEXT against the stored ISO
    # `created`. A caller with an epoch in hand (the natural thing) would get a lexicographic compare —
    # "2026-…" vs "1780272000" decides on the first character, '2' > '1', so EVERY real invoice reads
    # as outside the window and the response is a plausible, valid-cursored empty page. That silently
    # settled partner commission statements at 0.00. So a bound that is not ISO-8601 FAILS LOUDLY here.
    import datetime as _dt
    for _name, _val in (("since", since), ("until", until)):
        if not _val:
            continue
        try:
            _dt.datetime.fromisoformat(_val.replace("Z", "+00:00"))
        except ValueError:
            raise HTTPException(
                422,
                f"{_name} must be an ISO-8601 timestamp (e.g. 2026-06-15T09:00:00Z), not {_val!r}")
    await request.app.state.auditor.record(
        "billing.platform_invoices_viewed", actor=principal.subject, tenant=principal.tenant,
        resource="invoices:all-tenants", ip=request.client.host if request.client else "",
        metadata={"status": status_, "tenant": tenant, "since": since, "until": until})
    s = get_settings()
    try:
        return await (await build_billing(request)).list_invoices_platform(
            status=status_, subscription_id=subscription_id, tenant=tenant,
            since=since, until=until, limit=_page(limit, s), cursor=cursor)
    except ValueError as exc:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, str(exc)) from exc


@router.get("/admin/subscriptions", dependencies=[Depends(_PLATFORM), Depends(rate_limit)])
async def list_subscriptions_platform(request: Request,
                                      status_: str = Query(default="", alias="status", max_length=32),
                                      plan_id: str = Query(default="", max_length=64),
                                      tenant: str = Query(default="", max_length=128),
                                      payment_gateway: str = Query(default="", max_length=64),
                                      since: str = Query(default="", max_length=32),
                                      until: str = Query(default="", max_length=32),
                                      limit: int | None = Query(default=None, ge=1, le=1000),
                                      cursor: str | None = Query(default=None, max_length=512),
                                      principal: Principal = Depends(require_user)) -> dict:
    """Subscriptions ACROSS ALL TENANTS — a platform lifecycle view. platform.billing only.

    The exact counterpart of /admin/invoices, fenced identically: the capability is platform-only (a
    tenant admin gets 403, never their own rows silently); a filter is mandatory (no unbounded dump);
    it is paginated and capped; it is audited write-ahead (who looked across every tenant, and when);
    it returns a lifecycle projection, not buyer PII; and it is a DISTINCT route, so it reads as the
    privileged one rather than a magic parameter on the tenant-scoped list. Answers "how many
    new / active / canceled subscriptions this month" (created / status / canceled_at).
    """
    if not (status_ or plan_id or tenant or payment_gateway or since or until):
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "PLATFORM_LISTING_NEEDS_A_FILTER")
    # `since`/`until` bound an ISO-8601 CREATION date, compared AS TEXT against the stored ISO
    # `created`. An epoch bound would lexicographically compare "2026-…" vs "178…" and read EVERY
    # real row as outside the window — a plausible, valid-cursored empty page. So reject a non-ISO
    # bound LOUDLY here, exactly as the invoice route does.
    import datetime as _dt
    for _name, _val in (("since", since), ("until", until)):
        if not _val:
            continue
        try:
            _dt.datetime.fromisoformat(_val.replace("Z", "+00:00"))
        except ValueError:
            raise HTTPException(
                422,
                f"{_name} must be an ISO-8601 timestamp (e.g. 2026-06-15T09:00:00Z), not {_val!r}")
    await request.app.state.auditor.record(
        "billing.platform_subscriptions_viewed", actor=principal.subject, tenant=principal.tenant,
        resource="subscriptions:all-tenants", ip=request.client.host if request.client else "",
        metadata={"status": status_, "plan_id": plan_id, "tenant": tenant,
                  "payment_gateway": payment_gateway, "since": since, "until": until})
    s = get_settings()
    try:
        return await (await build_billing(request)).list_subscriptions_platform(
            status=status_, plan_id=plan_id, tenant=tenant, payment_gateway=payment_gateway,
            since=since, until=until, limit=_page(limit, s), cursor=cursor)
    except ValueError as exc:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, str(exc)) from exc


@router.post("/admin/subscriptions/{sub_id}/force-suspend", dependencies=[Depends(_PLATFORM)])
async def force_suspend_subscription(request: Request, sub_id: str, reason: str = Query(default="", max_length=500),
                                     principal: Principal = Depends(require_user)) -> dict:
    """Deliberately stop ONE subscription's renewals — the operator override for a subscriber stranded
    on an old gateway who has not re-entered payment. Platform-only, one at a time, and AUDITED
    write-ahead as a critical action (it stops someone's paid service; the row must exist even if the
    mutation then fails). Distinct from self-service pause; reactivate() resumes it once they update
    payment. The automatic behaviour stays 'keep renewing on the original gateway' — this is the
    manual exception, never a sweep."""
    svc = await build_billing(request)
    sub = await svc._subs.fetch(sub_id)
    if sub is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "NOT_FOUND")
    await request.app.state.auditor.record(
        "subscription.force_suspended", actor=principal.subject, tenant=sub.tenant,
        resource=f"subscription:{sub_id}", ip=request.client.host if request.client else "",
        metadata={"reason": reason, "gateway": sub.payment_gateway})
    try:
        updated = await svc.force_suspend(sub_id)
    except ValueError as e:
        raise HTTPException(status.HTTP_409_CONFLICT, str(e))
    return updated.to_dict()


def _pdf_response(rendered, filename: str) -> Response:
    if rendered is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "PDF_UNAVAILABLE")
    ct, data = rendered
    return Response(content=data, media_type=ct,
                    headers={"Content-Disposition": f'attachment; filename="{filename}"'})


async def _fetch_own(request: Request, invoice_id: str, principal: Principal):
    svc = await build_billing(request)
    inv = await svc._invoices.fetch(invoice_id)
    if inv is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "NOT_FOUND")
    _guard(principal, inv.tenant)
    return svc, inv


@router.get("/invoices/{invoice_id}/pdf")
async def invoice_pdf(request: Request, invoice_id: str, principal: Principal = Depends(_SELF)):
    """The TAX invoice. Served on the invoice's ISSUED type (the stamp), so it stays downloadable
    after the mode is switched back to receipt. Refused — and /document serves a receipt instead —
    when the live tax profile can no longer back a tax invoice (a seller registration emptied)."""
    svc, inv = await _fetch_own(request, invoice_id, principal)
    if svc.resolve_document(inv) != "tax_invoice":
        raise HTTPException(status.HTTP_409_CONFLICT, "TAX_INVOICE_NOT_ISSUED")
    return _pdf_response(await svc.render_pdf(inv), f"{inv.number}.pdf")


@router.get("/invoices/{invoice_id}/receipt")
async def invoice_receipt(request: Request, invoice_id: str, principal: Principal = Depends(_SELF)):
    """Our internal payment RECEIPT (non-tax), carrying the gateway reference. Available for any paid
    invoice that resolves to a receipt, was issued as one, or belongs to a `both`-mode tenant."""
    svc, inv = await _fetch_own(request, invoice_id, principal)
    if inv.status != "paid":
        raise HTTPException(status.HTTP_409_CONFLICT, "NOT_PAID")
    if not svc.receipt_available(inv):
        raise HTTPException(status.HTTP_409_CONFLICT, "RECEIPT_NOT_ISSUED")
    return _pdf_response(await svc.render_receipt(inv), f"{inv.number}-receipt.pdf")


@router.get("/invoices/{invoice_id}/document")
async def invoice_document(request: Request, invoice_id: str, principal: Principal = Depends(_SELF)):
    """Whatever THIS invoice IS — the type follows the INVOICE, not the current setting. The tax
    invoice if it was issued as one and the live profile still backs it; otherwise the receipt. So a
    pre-switch invoice downloads exactly what was originally issued, and a tax invoice whose seller
    number was later emptied falls back to a receipt rather than an invoice without a registration."""
    svc, inv = await _fetch_own(request, invoice_id, principal)
    if svc.resolve_document(inv) == "tax_invoice":
        return _pdf_response(await svc.render_pdf(inv), f"{inv.number}.pdf")
    if inv.status != "paid":
        raise HTTPException(status.HTTP_409_CONFLICT, "NOT_PAID")
    return _pdf_response(await svc.render_receipt(inv), f"{inv.number}-receipt.pdf")


@router.post("/payments/{payment_id}/refund")
async def refund(request: Request, payment_id: str, amount_minor: int | None = None,
                 principal: Principal = Depends(_ADMIN)) -> dict:
    # Resolve the payment and guard tenant OWNERSHIP before touching it — a tenant-scoped billing.manage
    # admin must never refund another tenant's payment (IDOR). platform.billing crosses tenants by design.
    svc = await build_billing(request)
    pay = await svc._payments.fetch(payment_id)
    if pay is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "NOT_FOUND")
    _guard(principal, pay.tenant)
    # Write-ahead: a critical action the log cannot take must not happen at all, so this
    # runs BEFORE the mutation. Recorded under the PAYMENT's tenant (not the caller's).
    await request.app.state.auditor.record(
        "payment.refunded", actor=principal.subject, tenant=pay.tenant,
        resource=f"payment:{payment_id}",
        ip=request.client.host if request.client else "")
    res = await svc.refund(payment_id, amount_minor)
    if not res.get("ok") and res.get("reason") == "NOT_FOUND":
        raise HTTPException(status.HTTP_404_NOT_FOUND, "NOT_FOUND")
    return res


@router.post("/payments/{payment_id}/reconcile")
async def reconcile_payment(request: Request, payment_id: str,
                            principal: Principal = Depends(_ADMIN)) -> dict:
    """Pull a payment's CURRENT status from the gateway and apply it — the recovery path when a
    webhook was missed or failed verification, leaving an invoice unpaid though the money moved.
    Idempotent (same seam as the webhook); never invents a status."""
    svc = await build_billing(request)
    pay = await svc._payments.fetch(payment_id)
    if pay is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "NOT_FOUND")
    _guard(principal, pay.tenant)                      # tenant ownership — no cross-tenant reconcile
    await request.app.state.auditor.record(
        "payment.reconciled", actor=principal.subject, tenant=pay.tenant,
        resource=f"payment:{payment_id}", ip=request.client.host if request.client else "")
    res = await svc.reconcile_payment(payment_id)
    if not res.get("ok") and res.get("reason") == "NOT_FOUND":
        raise HTTPException(status.HTTP_404_NOT_FOUND, "NOT_FOUND")
    return res


@router.get("/admin/wallet-topups/pending", dependencies=[Depends(_PLATFORM), Depends(rate_limit)])
async def list_pending_topups(request: Request,
                              limit: int | None = Query(default=None, ge=1, le=200),
                              older_than_seconds: int = Query(default=0, ge=0, le=86400),
                              cursor: str | None = Query(default=None, max_length=512),
                              principal: Principal = Depends(require_user)) -> dict:
    """PENDING wallet-topup payments awaiting a reconcile poll — the queue an operations poller walks,
    then calls POST /payments/{id}/reconcile per row (hosted-checkout gateways like TuwaiqPay have no
    signed webhook). platform.billing only. `older_than_seconds` skips just-opened sessions so the
    first poll gives the gateway a moment. Returns only top-up payments, never ordinary charges."""
    return await (await build_billing(request)).list_pending_topups(
        limit=limit or 50, older_than_seconds=older_than_seconds, after=cursor)


@router.post("/dunning/run")
async def dunning(request: Request, principal: Principal = Depends(_ADMIN)) -> dict:
    result = await (await build_billing(request)).dunning_sweep()
    await request.app.state.auditor.record(
        "dunning.run", actor=principal.subject, tenant=principal.tenant,
        resource="dunning", ip=request.client.host if request.client else "")
    return result


@router.post("/subscriptions/{sub_id}/migrate")
async def migrate_terms(request: Request, sub_id: str,
                        principal: Principal = Depends(_ADMIN)) -> dict:
    """Explicitly move a grandfathered subscription onto the CURRENT plan terms."""
    svc = await build_billing(request)
    sub = await svc._subs.fetch(sub_id)
    if sub is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "NOT_FOUND")
    _guard(principal, sub.tenant)                      # no repricing another tenant's grandfathered sub
    try:
        result = await svc.migrate_terms(sub_id)
    except ValueError as e:
        raise HTTPException(status.HTTP_404_NOT_FOUND, str(e))
    await request.app.state.auditor.record(
        "subscription.migrated", actor=principal.subject, tenant=sub.tenant,
        resource=f"subscription:{sub_id}", ip=request.client.host if request.client else "")
    return result


@router.post("/subscriptions/{sub_id}/overage")
async def run_overage(request: Request, sub_id: str, principal: Principal = Depends(_ADMIN)) -> dict:
    svc = await build_billing(request)
    sub = await svc._subs.fetch(sub_id)
    if sub is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "NOT_FOUND")
    _guard(principal, sub.tenant)                      # no forcing an overage charge cross-tenant
    inv = await svc.run_overage(sub_id)
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
    return await (await build_usage(request)).check(tenant=_tenant(principal), key=key, amount=amount)


@router.get("/usage", dependencies=[Depends(_SELF)])
async def usage_summary(request: Request, principal: Principal = Depends(_SELF)) -> dict:
    """'18 of 25 seats'. Without this the client has to count, which is how it went wrong."""
    return await (await build_usage(request)).summary(_tenant(principal))


@router.get("/admin/usage", dependencies=[Depends(_PLATFORM), Depends(rate_limit)])
async def usage_summary_platform(request: Request,
                                 tenant: str = Query(..., min_length=1, max_length=128),
                                 principal: Principal = Depends(require_user)) -> dict:
    """A NAMED tenant's usage/limits ('18 of 25 seats') — the platform-admin twin of /usage, for a
    platform console showing another tenant's entitlements. Same safety as /admin/invoices +
    /admin/subscriptions: platform.billing only (a tenant admin gets 403, never another tenant's
    numbers silently); `tenant` is MANDATORY and names exactly whose usage to read; audited
    write-ahead (who looked across the wall, and when); a read-only entitlement projection, no PII;
    a DISTINCT route, so it reads as the privileged one, not a magic parameter on /usage. Returns
    exactly what summary()/resolve() already return (limits + detail + provenance)."""
    await request.app.state.auditor.record(
        "billing.platform_usage_viewed", actor=principal.subject, tenant=principal.tenant,
        resource=f"usage:{tenant}", ip=request.client.host if request.client else "",
        metadata={"tenant": tenant})
    return await (await build_usage(request)).summary(tenant)


@router.get("/ledger", dependencies=[Depends(_ADMIN)])
async def ledger(request: Request, principal: Principal = Depends(_ADMIN)) -> dict:
    return {"ledger": await (await build_billing(request))._ledger.list(_tenant(principal),
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
    svc = (await build_billing(request))
    tenant = None if "platform.billing" in principal.caps else (principal.tenant or "__none__")
    try:
        result = await svc.settle(invoice_id=invoice_id, tenant=tenant, **body.model_dump())
    except _stl.SettlementError as e:
        raise _settlement_error(e) from e
    await request.app.state.auditor.record(
        "invoice.settled", actor=principal.subject, tenant=principal.tenant or "",
        resource=f"invoice:{invoice_id}", ip=request.client.host if request.client else "")
    return result


@router.get("/invoices/{invoice_id}/settlements", dependencies=[Depends(_SELF)])
async def list_settlements(request: Request, invoice_id: str,
                           principal: Principal = Depends(_SELF)) -> dict:
    svc = (await build_billing(request))
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
    svc = (await build_billing(request))
    tenant = None if "platform.billing" in principal.caps else (principal.tenant or "__none__")
    try:
        return await svc.reverse_settlement(settlement_id=settlement_id, tenant=tenant,
                                            reason=body.reason, actor=body.actor)
    except _stl.SettlementError as e:
        raise _settlement_error(e) from e
