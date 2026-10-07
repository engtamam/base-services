# =============================================================================
# billing-service — Internal tier (service-to-service, X-Internal-Token)
# -----------------------------------------------------------------------------
# NOT user-facing. Guarded by a shared token compared in CONSTANT TIME
# (CWE-208: observable timing discrepancy). Never a user JWT.
# =============================================================================
from __future__ import annotations


from fastapi import APIRouter, Depends, Header, HTTPException, Request, status
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

# The refusal codes attach_coupon may raise, exposed as a machine-readable `reason` on the
# attach-coupon endpoint so the caller can show the shop the real cause. A WHITELIST: only codes we
# emit are echoed; anything else collapses to the generic COUPON_REFUSED, so an unexpected message
# can never leak through.
_ATTACH_REASONS = frozenset({
    "COUPON_CODE_REQUIRED", "COUPON_PLAN_MISMATCH", "COUPON_UNKNOWN", "COUPON_EXPIRED",
    "COUPON_EXHAUSTED", "COUPON_DISABLED", "COUPON_SCHEDULED", "COUPON_MAX_REDEMPTIONS",
})

from app.api.v1.billing import CartItemIn, build_billing, build_usage
from app.config import Settings, get_settings
from app.core.erasure import BillingErasure
from app.core import backup as _backup
from app.backup_manifest import BACKUP
from app.manifest import build_manifest
from app.core.pii import subject_ref

router = APIRouter(prefix="/internal", tags=["internal"])

_HEADER = "X-Internal-Token"
_ASSERTION_HEADER = "X-Internal-Assertion"


async def require_internal_token(
    request: Request,
    x_internal_token: str | None = Header(default=None, alias=_HEADER),
    x_internal_assertion: str | None = Header(default=None, alias=_ASSERTION_HEADER),
    settings: Settings = Depends(get_settings),
) -> None:
    """Authenticate the internal caller, then authorise it for THIS route. The name is unchanged so
    every existing `Depends(require_internal_token)` keeps working, but it now does two things:

      1. IDENTITY — a config-resolved (cached) driver verifies the caller. `shared` (default) is
         the timing-safe flat-token compare (today's behaviour, unnamed peer); `static`/`jwks` verify a
         per-caller RS256 assertion. Any failure is one generic 401 — never which check failed.
      2. POLICY — the per-route allow-list decides whether that caller may reach `request.url.path`. A
         route with no policy admits any authenticated caller (default-open), so identity alone never
         breaks a path; a named allow-list yields 403 for a caller not on it.

    The verifier + policy are resolved from config (cached), not the container, so this seam is
    identical in every service."""
    from app.core.internal_caller_factory import resolve_internal_caller, resolve_internal_policy
    from app.ports.internal_caller import InternalCallerError

    audience = settings.INTERNAL_CALLER_AUDIENCE or settings.SERVICE_NAME
    try:
        identity = resolve_internal_caller(settings).verify(
            token=x_internal_token or "", assertion=x_internal_assertion or "", audience=audience)
    except InternalCallerError:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "UNAUTHORIZED")
    if not resolve_internal_policy(settings).permits(request.url.path, identity.caller):
        raise HTTPException(status.HTTP_403_FORBIDDEN, "FORBIDDEN")


@router.get("/health", dependencies=[Depends(require_internal_token)])
async def internal_health(settings: Settings = Depends(get_settings)) -> dict[str, str]:
    return {"status": "ok", "service": settings.SERVICE_NAME}


def _webhook_tenant(request: Request) -> str:
    """A gateway callback carries no session, so the tenant comes from the path it was registered
    under. Platform-wide is the correct default: most deployments hold one gateway account."""
    return request.query_params.get("tenant", "") or ""


@router.post("/billing/webhook/{provider}")
async def payment_webhook(provider: str, request: Request,
                          settings: Settings = Depends(get_settings)) -> dict:
    """Gateway webhook — signature-verified inside handle_webhook (no token gate; the
    signature IS the auth). Reconciles the payment/invoice idempotently."""
    body = await request.body()
    headers = {k.lower(): v for k, v in request.headers.items()}
    # Every signing secret currently valid, not just the newest. A callback already in flight was
    # signed with whatever the gateway held when it sent; rejecting it as forged would leave an
    # invoice unpaid after the customer's money has already moved.
    resolver = getattr(request.app.state, "credentials", None)
    wh_tenant = _webhook_tenant(request)
    secrets = (await resolver.webhook_secrets(wh_tenant)
               if resolver is not None else settings.PAYMENT_WEBHOOK_SECRET)
    # Bind the secret's tenant to the payment's tenant: a party holding tenant A's signing secret must
    # not drive tenant B's payment by passing ?tenant=A with a known provider_id belonging to B.
    result = await (await build_billing(request)).handle_webhook(
        provider, headers, body, secrets, webhook_tenant=wh_tenant)
    if not result.get("ok"):
        raise HTTPException(status.HTTP_400_BAD_REQUEST, result.get("reason", "INVALID"))
    return result


@router.post("/billing/dunning", dependencies=[Depends(require_internal_token)])
async def run_dunning(request: Request) -> dict:
    return await (await build_billing(request)).dunning_sweep()


@router.post("/billing/cycle-due", dependencies=[Depends(require_internal_token)])
async def cycle_due(request: Request) -> dict:
    """Invoice every subscription whose period has ended — the renewal engine.

    Driven by a daily cron beside dunning. Bounded, idempotent per cycle, and resilient to a single
    unbillable subscription. The `<= now` guard lives in the service, never here: run_cycle charges
    unconditionally, so the filter is the only thing between "renew what is due" and "charge
    everyone today".
    """
    return await (await build_billing(request)).cycle_due_sweep()


@router.post("/billing/expiry-warn-sweep", dependencies=[Depends(require_internal_token)])
async def expiry_warn_sweep(request: Request) -> dict:
    """Emit subscription.expiring_soon for subscriptions whose period ends within the warning window.

    Driven by a daily cron beside cycle-due. Bounded/paged, idempotent per (subscription, period_end)
    so it fires exactly once per period, and re-arms when the period rolls forward on renewal. billing
    delivers nothing itself — only the bus event, for a consumer to turn into a reminder.
    """
    return await (await build_billing(request)).expiry_warn_sweep()


class ConsumeIn(BaseModel):
    tenant: str = Field(min_length=1, max_length=200)
    key: str = Field(min_length=1, max_length=200)
    amount: int = Field(default=1, ge=0)
    idempotency_key: str | None = Field(default=None, max_length=200)


class SetUsageIn(BaseModel):
    tenant: str = Field(min_length=1, max_length=200)
    key: str = Field(min_length=1, max_length=200)
    value: int = Field(ge=0)


@router.get("/billing/entitlements/check", dependencies=[Depends(require_internal_token)])
async def entitlement_check(request: Request, tenant: str, key: str, amount: int = 1) -> dict:
    """ADVISORY: 'may this tenant do <key>?' Usage is read server-side (fail-closed).

    No `current` parameter: the caller no longer states its own usage. And this is advisory
    even so — between a check that says yes and the action, another request can take the last
    unit. To ENFORCE, call /usage/consume, which checks and increments atomically.
    """
    return await (await build_usage(request)).check(tenant=tenant, key=key, amount=amount)


@router.get("/billing/subscriptions", dependencies=[Depends(require_internal_token)])
async def list_subscriptions_internal(request: Request, tenant: str,
                                      settings: Settings = Depends(get_settings)) -> dict:
    """Every subscription a tenant currently HOLDS — the exact billable set EntitlementResolver
    computes over — for a peer product service acting on behalf of a shop.

    WHY IT EXISTS: a base-plus-apps catalogue turns on one structural fact — what does this tenant
    hold right now? (one base per tenant; an app bought once). GET /api/v1/billing/subscriptions is
    _SELF, so a service is not a clean caller; and encoding "has a base" as a feature flag loses WHICH
    subscription, WHEN it renews and WHAT was paid. This returns exactly the fields those rules need —
    id, plan_id, status, current_period_end, limits_snapshot — and NOTHING else: the payment token and
    the buyer's tax identity on the row never cross the internal API. It is the same status-filtered,
    indexed read the resolver runs (SubscriptionStore.billable_for), so the two never disagree.
    """
    if not tenant:
        raise HTTPException(400, "tenant is required")
    from app.core.stores import SubscriptionStore
    c = request.app.state.container
    rows = await SubscriptionStore(c.datastore, c.cache).billable_for(
        tenant, tuple(settings.billable_statuses), limit=settings.MAX_BILLABLE_SUBSCRIPTIONS)
    return {"subscriptions": [
        {"id": r.get("id", ""), "plan_id": r.get("plan_id", ""), "status": r.get("status", ""),
         "current_period_end": float(r.get("current_period_end", 0) or 0),
         "limits_snapshot": r.get("limits_snapshot") or {}}
        for r in rows]}


# The horizon a caller may look ahead — capped so a huge `within_seconds` cannot turn a bounded
# read into an effectively-unbounded scan of the whole active set. 90 days covers any renewal cadence.
_RENEWING_SOON_MAX_WITHIN = 90 * 86_400
_RENEWING_SOON_MAX_LIMIT = 500


@router.get("/billing/subscriptions/renewing-soon", dependencies=[Depends(require_internal_token)])
async def renewing_soon_internal(request: Request, within_seconds: int = 172_800,
                                 limit: int = 100, cursor: str = "") -> dict:
    """READ-ONLY: subscriptions across ALL tenants whose period ends within `within_seconds` — the set
    a proactive pre-renewal reminder (e.g. "renews in 2 days") iterates. NO side effect: it never
    invoices and never mutates a subscription — deliberately NOT the cycle sweep, which does. Returns
    the SAME safe fields as /internal/billing/subscriptions plus `tenant` (this view spans tenants),
    and NOTHING sensitive (no payment token, no buyer tax identity). Real keyset paging via `cursor`
    (a call may return fewer than `limit` while still handing back a cursor — page until it is empty).

    NOTE ON SEMANTICS: a row is included when 0 < current_period_end <= now+within_seconds — the same
    "period has (or will have) ended by the horizon" test the sweep uses, extended into the future. It
    can therefore include a just-lapsed-but-not-yet-swept renewal; a caller that wants strictly-future
    renewals can compare current_period_end against its own clock.
    """
    if within_seconds <= 0 or within_seconds > _RENEWING_SOON_MAX_WITHIN:
        raise HTTPException(400, f"within_seconds must be 1..{_RENEWING_SOON_MAX_WITHIN}")
    limit = max(1, min(int(limit), _RENEWING_SOON_MAX_LIMIT))
    from app.core.models import now_ts
    from app.core.stores import SubscriptionStore
    c = request.app.state.container
    horizon = now_ts() + within_seconds
    rows, nxt = await SubscriptionStore(c.datastore, c.cache).renewing_soon(
        horizon, limit=limit, cursor=cursor or None)
    return {"subscriptions": [
        {"id": r.get("id", ""), "tenant": r.get("tenant", ""), "plan_id": r.get("plan_id", ""),
         "status": r.get("status", ""),
         "current_period_end": float(r.get("current_period_end", 0) or 0),
         "limits_snapshot": r.get("limits_snapshot") or {}}
        for r in rows], "next_cursor": nxt}


# Money-relevant states only: 'paid' (a peer accrues on it) and 'void' (it claws back). draft/open are
# internal working states and are never exposed across the internal API.
_INVOICE_HISTORY_STATUSES = ("paid", "void")


@router.get("/billing/invoices", dependencies=[Depends(require_internal_token)])
async def list_invoices_internal(request: Request, tenant: str, status: str = "paid",
                                 settings: Settings = Depends(get_settings)) -> dict:
    """A tenant's PAID (or voided) invoice history for a peer service that must key MONEY on it.

    WHY IT EXISTS: events.py's own contract says the at-most-once bus is NOT how a money consequence
    is delivered — "anything that MUST happen ... reads the fact back; the event only makes that path
    faster". This is that durable, idempotent read. A commission engine (affiliate-service) accrues on
    a real payment, holds for N days from paid_at, and pays a renewal at the Nth cycle — none of which
    may silently lose a payout because a subscriber was redeploying when the event fired.

    PRIVACY — the same discipline as events.payment_payload(): the amount and the payment DATE, and
    nothing that is not the caller's to hold. NO buyer name, NO tax identity (vat/cr), NO line items,
    NO invoice number. tenant + invoice id are enough to look up more from a service allowed to.

    Each row carries paid_sequence (1 = the tenant's FIRST-ever payment — the conversion) and
    subscription_paid_sequence (1 = the first payment of THAT subscription; N = its (N-1)th renewal),
    so the caller distinguishes a conversion from an Nth renewal without a second read. refunded_minor
    travels too, so a commission is never paid on money that was refunded. One bounded, indexed read
    (never a scan); `truncated` is the loud signal for a history past the cap."""
    if not tenant:
        raise HTTPException(400, "tenant is required")
    if status not in _INVOICE_HISTORY_STATUSES:
        raise HTTPException(400, f"status must be one of {', '.join(_INVOICE_HISTORY_STATUSES)}")
    from app.core.stores import InvoiceStore
    c = request.app.state.container
    rows, truncated = await InvoiceStore(c.datastore).history_for_tenant(
        tenant, status, settings.INTERNAL_INVOICE_HISTORY_MAX)
    per_sub: dict[str, int] = {}
    invoices: list[dict] = []
    for i, r in enumerate(rows, start=1):
        sub = r.get("subscription_id", "") or ""
        per_sub[sub] = per_sub.get(sub, 0) + 1
        invoices.append({
            "id": r.get("id", ""),
            "subscription_id": sub,
            "status": r.get("status", ""),
            "paid_at": r.get("paid_at", "") or "",
            "voided_at": r.get("voided_at", "") or "",      # WHEN it voided (full/accumulated refund), "" if never
            "amount_minor": int(r.get("total_minor", 0) or 0),
            "subtotal_minor": int(r.get("subtotal_minor", 0) or 0),  # the VAT split, for a dispute report (ح-2)
            "tax_minor": int(r.get("tax_minor", 0) or 0),
            "refunded_minor": int(r.get("refunded_minor", 0) or 0),
            "currency": r.get("currency", "") or "",
            "paid_sequence": i,                             # tenant-wide; 1 = the conversion
            "subscription_paid_sequence": per_sub[sub],     # per-subscription; N-1 renewals before it
        })
    return {"tenant": tenant, "status": status, "invoices": invoices, "truncated": truncated}


# States a caller may still COLLECT on — the only ones this read exposes. A settled invoice (paid /
# void / uncollectible) is money HISTORY and stays behind the tenant-scoped history read above, which
# deliberately omits line items; this read is the opposite consumer — the amount a payer is about to
# pay, shown on a checkout/Flow screen, so it DOES carry the lines. Keeping the two contracts apart is
# the privacy boundary: settled detail never leaks through the "what will this cost me" path.
_INVOICE_PAYABLE_STATUSES = ("draft", "open", "partially_paid")


@router.get("/billing/invoices/{invoice_id}", dependencies=[Depends(require_internal_token)])
async def get_payable_invoice_internal(request: Request, invoice_id: str, tenant: str) -> dict:
    """One OPEN (not-yet-settled) invoice's amount + lines, for a peer that must show a customer what a
    renewal will cost BEFORE they pay it (a WhatsApp Flow / checkout screen).

    tenant is REQUIRED and is the IDOR guard: an invoice whose tenant differs is reported as absent
    (404), never confirmed to exist for another tenant. Only a payable invoice is returned — a settled
    one (paid/void/uncollectible) is money history and is read through GET /billing/invoices instead,
    so this path never becomes a second, line-leaking window onto settled invoices. amount_due_minor is
    total minus what has already settled (a partial payment), which is what the payer still owes."""
    if not tenant:
        raise HTTPException(400, "tenant is required")
    from app.core.stores import InvoiceStore
    c = request.app.state.container
    inv = await InvoiceStore(c.datastore).fetch(invoice_id)
    if inv is None or inv.tenant != tenant or inv.status not in _INVOICE_PAYABLE_STATUSES:
        raise HTTPException(404, "invoice not found")
    lines = [{"description": li.get("description", ""), "quantity": int(li.get("quantity", 1) or 1),
              "unit_minor": int(li.get("unit_minor", 0) or 0),
              "amount_minor": int(li.get("amount_minor", 0) or 0),
              **({"proration": li["proration"]} if li.get("proration") is not None else {})}
             for li in (inv.lines or [])]
    settled = await (await build_billing(request, tenant)).settled_minor(inv.id)
    total = int(inv.total_minor or 0)
    return {
        "id": inv.id, "tenant": inv.tenant, "subscription_id": inv.subscription_id or "",
        "status": inv.status, "currency": inv.currency or "",
        "subtotal_minor": int(inv.subtotal_minor or 0), "discount_minor": int(inv.discount_minor or 0),
        "tax_minor": int(inv.tax_minor or 0), "total_minor": total,
        "settled_minor": settled, "amount_due_minor": max(0, total - settled),
        "consolidated": bool((inv.meta or {}).get("consolidated")),
        "lines": lines, "created": inv.created or "",
    }


@router.get("/billing/plans", dependencies=[Depends(require_internal_token)])
async def list_plans_internal(request: Request, tenant: str = "",
                              include_hidden: bool = False) -> dict:
    """The internal twin of GET /api/v1/billing/plans — the purchasable plan catalogue, for a peer
    service (operations during signup / the purchase flow). The user route is _SELF and rate-limited
    per caller; a service presents no subject, so the limiter falls back to the operations pod's IP
    and every establishment shares one bucket — 429s on the purchase path that must fail closed. This
    is the same read behind the internal token, tenant an explicit field. `include_hidden` (retired /
    unlisted pricing) is an operator view; the internal token is platform authority, so it is honoured
    here rather than forced to a person's capability."""
    return {"plans": await (await build_billing(request, tenant)).list_plans(
        include_hidden=include_hidden)}


class SubscribeInternalIn(BaseModel):
    """Same shape as POST /api/v1/billing/subscriptions, but the TENANT rides in the body — the caller
    is a product service acting FOR a shop, not a human whose token carries a tenant."""
    tenant: str = Field(min_length=1, max_length=128)
    plan_id: str = Field(min_length=1, max_length=128)
    interval: str = Field(default="month", pattern="^(month|year|once)$")
    quantity: int = Field(default=1, ge=1)
    coupon_code: str = Field(default="", max_length=128)
    payment_token: str = Field(default="", max_length=512)
    buyer_name: str = Field(default="", max_length=256)
    buyer_vat: str = Field(default="", max_length=64)
    buyer_cr: str = Field(default="", max_length=64)
    idempotency_key: str = Field(default="", max_length=128)


@router.post("/billing/subscriptions", status_code=201,
             dependencies=[Depends(require_internal_token)])
async def subscribe_internal(request: Request, body: SubscribeInternalIn) -> dict:
    """The internal twin of POST /api/v1/billing/subscriptions — create a subscription for a shop,
    service-to-service. Same core .subscribe(); tenant in the body; the SAME grandfathering, coupon
    and proration behaviour as the user route. Exists because the purchase is the one path that must
    NOT 429 on a shared per-pod IP bucket (see list_plans_internal); the internal token is the auth
    every other cross-service call already uses."""
    try:
        res = await (await build_billing(request, body.tenant)).subscribe_with_payment(
            tenant=body.tenant, plan_id=body.plan_id, interval=body.interval,
            quantity=body.quantity, coupon_code=body.coupon_code, payment_token=body.payment_token,
            buyer_name=body.buyer_name, buyer_vat=body.buyer_vat, buyer_cr=body.buyer_cr,
            idempotency_key=body.idempotency_key or None)
    except ValueError as exc:
        msg = str(exc)
        if msg == "PAYMENT_REQUIRED":
            raise HTTPException(status.HTTP_402_PAYMENT_REQUIRED, msg) from exc
        if msg in ("PLAN_NOT_FOUND", "PLAN_NOT_AVAILABLE"):
            raise HTTPException(status.HTTP_404_NOT_FOUND, msg) from exc
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, msg) from exc
    await request.app.state.auditor.record(
        "subscription.created", actor="", tenant=res.get("tenant") or body.tenant,
        resource=f"subscription:{res.get('id') or '-'}", ip=request.client.host if request.client else "",
        metadata={"plan": body.plan_id, "paid": str(res.get("paid", ""))})
    return res


class CheckoutInternalIn(BaseModel):
    """Same shape as POST /api/v1/billing/subscriptions/checkout, but the TENANT rides in the body —
    the caller is a product service acting FOR a shop, not a human whose token carries a tenant."""
    tenant: str = Field(min_length=1, max_length=128)
    items: list[CartItemIn] = Field(min_length=1, max_length=50)
    payment_token: str = Field(default="", max_length=512)
    buyer_name: str = Field(default="", max_length=256)
    buyer_vat: str = Field(default="", max_length=64)
    buyer_cr: str = Field(default="", max_length=64)
    idempotency_key: str = Field(default="", max_length=128)


@router.post("/billing/subscriptions/checkout", status_code=201,
             dependencies=[Depends(require_internal_token)])
async def checkout_internal(request: Request, body: CheckoutInternalIn) -> dict:
    """The internal twin of POST /api/v1/billing/subscriptions/checkout — atomic multi-plan checkout,
    service-to-service. Same core .checkout() (all validation, one-invoice-one-charge, idempotency and
    per-item semantics are identical); tenant in the body; X-Internal-Token auth. No new business
    logic — exactly the relationship subscribe_internal has to subscribe."""
    try:
        result = await (await build_billing(request, body.tenant)).checkout(
            tenant=body.tenant, items=[i.model_dump() for i in body.items],
            payment_token=body.payment_token, buyer_name=body.buyer_name, buyer_vat=body.buyer_vat,
            buyer_cr=body.buyer_cr, idempotency_key=body.idempotency_key)
    except ValueError as exc:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, str(exc)) from exc
    await request.app.state.auditor.record(
        "subscription.checkout", actor="", tenant=result["tenant"],
        resource=f"invoice:{result.get('invoice_id') or '-'}",
        ip=request.client.host if request.client else "",
        metadata={"plans": ",".join(i["plan_id"] for i in result["items"]),
                  "paid": str(result["paid"])})
    return result


class SubscriptionCheckoutLinkInternalIn(BaseModel):
    """Same shape as POST /api/v1/billing/subscriptions/checkout-link, tenant in the body — a product
    service (e.g. operations) starting a hosted-checkout subscription FOR a shop."""
    tenant: str = Field(min_length=1, max_length=128)
    plan_id: str = Field(min_length=1, max_length=128)
    interval: str = Field(default="month", pattern="^(month|year|once)$")
    quantity: int = Field(default=1, ge=1, le=1000)
    coupon_code: str = Field(default="", max_length=128)
    buyer_name: str = Field(default="", max_length=256)
    buyer_vat: str = Field(default="", max_length=64)
    buyer_cr: str = Field(default="", max_length=64)
    return_url: str = Field(default="", max_length=1024)
    ttl_seconds: int | None = Field(default=None, ge=0)
    idempotency_key: str = Field(default="", max_length=128)


@router.post("/billing/subscriptions/checkout-link", status_code=201,
             dependencies=[Depends(require_internal_token)])
async def start_subscription_checkout_internal(
        request: Request, body: SubscriptionCheckoutLinkInternalIn,
        settings: Settings = Depends(get_settings)) -> dict:
    """Internal twin of POST /api/v1/billing/subscriptions/checkout-link — start a new paid
    subscription that returns a hosted-checkout link, service-to-service. Same core
    .start_subscription_checkout(); tenant in the body; X-Internal-Token auth. No new business logic —
    exactly the relationship subscribe_internal/checkout_internal have to their user routes."""
    from app.core.billing_service import UpgradeChargeFailed, UpgradeChargeUnavailable
    try:
        res = await (await build_billing(request, body.tenant)).start_subscription_checkout(
            tenant=body.tenant, plan_id=body.plan_id, interval=body.interval, quantity=body.quantity,
            coupon_code=body.coupon_code, buyer_name=body.buyer_name, buyer_vat=body.buyer_vat,
            buyer_cr=body.buyer_cr, return_url=body.return_url,
            ttl_seconds=int(body.ttl_seconds or 0), idempotency_key=body.idempotency_key)
    except ValueError as exc:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, str(exc)) from exc
    except UpgradeChargeUnavailable as exc:
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, "GATEWAY_UNAVAILABLE") from exc
    except UpgradeChargeFailed as exc:
        raise HTTPException(status.HTTP_502_BAD_GATEWAY, str(exc) or "CHECKOUT_FAILED") from exc
    await request.app.state.auditor.record(
        "subscription.checkout_link_created", actor="", tenant=body.tenant,
        resource=f"subscription:{res['subscription_id']}",
        ip=request.client.host if request.client else "",
        metadata={"plan": body.plan_id, "invoice_id": res.get("invoice_id") or "-"})
    if res.get("checkout_required") and res.get("token"):
        base = (settings.BILLING_PAY_LINK_BASE_URL or "").strip()
        res["pay_url"] = (base.rstrip("/") + "/" + res["token"]) if base else res["checkout_url"]
    return res


class CartCheckoutLinkInternalIn(BaseModel):
    """Same shape as checkout_internal (items[] + tenant), but returns ONE hosted-checkout link for the
    cart instead of charging a token — for token-less gateways."""
    tenant: str = Field(min_length=1, max_length=128)
    items: list[CartItemIn] = Field(min_length=1, max_length=50)
    buyer_name: str = Field(default="", max_length=256)
    buyer_vat: str = Field(default="", max_length=64)
    buyer_cr: str = Field(default="", max_length=64)
    return_url: str = Field(default="", max_length=1024)
    ttl_seconds: int | None = Field(default=None, ge=0)
    idempotency_key: str = Field(default="", max_length=128)


@router.post("/billing/subscriptions/checkout/link", status_code=201,
             dependencies=[Depends(require_internal_token)])
async def start_cart_checkout_internal(request: Request, body: CartCheckoutLinkInternalIn,
                                       settings: Settings = Depends(get_settings)) -> dict:
    """Internal twin of POST /api/v1/billing/subscriptions/checkout/link — an atomic multi-plan cart
    that returns ONE hosted-checkout link, service-to-service. Same core .start_cart_checkout(); tenant
    in the body; X-Internal-Token auth. The token-less counterpart of checkout_internal."""
    from app.core.billing_service import UpgradeChargeFailed, UpgradeChargeUnavailable
    try:
        res = await (await build_billing(request, body.tenant)).start_cart_checkout(
            tenant=body.tenant, items=[i.model_dump() for i in body.items], buyer_name=body.buyer_name,
            buyer_vat=body.buyer_vat, buyer_cr=body.buyer_cr, return_url=body.return_url,
            ttl_seconds=int(body.ttl_seconds or 0), idempotency_key=body.idempotency_key)
    except ValueError as exc:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, str(exc)) from exc
    except UpgradeChargeUnavailable as exc:
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, "GATEWAY_UNAVAILABLE") from exc
    except UpgradeChargeFailed as exc:
        raise HTTPException(status.HTTP_502_BAD_GATEWAY, str(exc) or "CHECKOUT_FAILED") from exc
    await request.app.state.auditor.record(
        "subscription.checkout_link_created", actor="", tenant=body.tenant,
        resource=f"invoice:{res.get('invoice_id') or '-'}",
        ip=request.client.host if request.client else "",
        metadata={"plans": ",".join(i["plan_id"] for i in res["items"])})
    if res.get("checkout_required") and res.get("token"):
        base = (settings.BILLING_PAY_LINK_BASE_URL or "").strip()
        res["pay_url"] = (base.rstrip("/") + "/" + res["token"]) if base else res["checkout_url"]
    return res


class CheckoutPreviewInternalIn(BaseModel):
    """Same shape as the user checkout preview, tenant in the body."""
    tenant: str = Field(min_length=1, max_length=128)
    items: list[CartItemIn] = Field(min_length=1, max_length=50)
    buyer_vat: str = Field(default="", max_length=64)


@router.post("/billing/subscriptions/checkout/preview",
             dependencies=[Depends(require_internal_token)])
async def checkout_preview_internal(request: Request, body: CheckoutPreviewInternalIn) -> dict:
    """Internal twin of the pre-purchase cart price preview — read-only, tenant in the body,
    X-Internal-Token auth. Same core .checkout_preview(); creates and charges nothing."""
    try:
        return await (await build_billing(request, body.tenant)).checkout_preview(
            tenant=body.tenant, items=[i.model_dump() for i in body.items], buyer_vat=body.buyer_vat)
    except ValueError as exc:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, str(exc)) from exc


class CouponCreateInternalIn(BaseModel):
    """Create a coupon service-to-service (a peer acting for a shop, e.g. operations minting a
    one-time grant code). Same fields as the admin CouponIn EXCEPT `public`: a scoped creator may
    never publish a code to the unauthenticated /offers list, so the field simply does not exist here
    — it is forced false in the service. A percent `value` over 100 is refused (COUPON_PERCENT_OUT_OF
    _RANGE), the one coupon-value cap billing enforces for every creator."""
    code: str = Field(min_length=1, max_length=64)
    type: str = Field(default="percent", pattern="^(percent|fixed|trial)$")
    value: int = Field(default=0, ge=0)
    currency: str = Field(default="SAR", max_length=8)
    starts_at: float | None = None
    expires_at: float | None = None
    max_redemptions: int = Field(default=0, ge=0)     # 0 = unlimited
    plan_id: str = Field(default="", max_length=128)  # scope to ONE plan; empty = any plan
    active: bool = True
    note: str = Field(default="", max_length=512)


@router.post("/billing/coupons", status_code=201, dependencies=[Depends(require_internal_token)])
async def create_coupon_internal(request: Request, body: CouponCreateInternalIn) -> dict:
    """Create a coupon over the internal token — the narrow primitive a peer service needs to mint a
    grant code, WITHOUT the broad platform.billing/billing.manage capability (which grants full
    coupon/discount-rule/plan CRUD). This route does exactly one thing: create one coupon row, always
    private (`public` cannot be set), with the percent cap enforced. No read/update/delete, no
    discount-rule or plan access — the whole blast radius is 'one coupon row'."""
    try:
        coupon = await (await build_billing(request)).create_scoped_coupon(**body.model_dump())
    except ValueError as exc:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, str(exc)) from exc
    # Recorded after success so a rejected create is not logged as one; source "internal" distinguishes
    # this from an admin coupon.created.
    await request.app.state.auditor.record(
        "coupon.created_scoped", actor="", tenant="",
        resource=f"coupon:{body.code}", ip=request.client.host if request.client else "",
        metadata={"type": body.type, "plan_id": body.plan_id})
    return coupon.to_dict()


class AttachCouponInternalIn(BaseModel):
    """Attach/replace a coupon on an EXISTING subscription, service-to-service. `tenant` is REQUIRED
    and is the IDOR guard (same rule as cancel_internal): a subscription whose tenant differs is
    reported absent, never mutated for another tenant."""
    tenant: str = Field(min_length=1, max_length=128)
    coupon_code: str = Field(min_length=1, max_length=64)


@router.post("/billing/subscriptions/{sub_id}/coupon",
             dependencies=[Depends(require_internal_token)])
async def attach_coupon_internal(request: Request, sub_id: str,
                                 body: AttachCouponInternalIn) -> dict:
    """Attach (or replace) a coupon code on an existing subscription — the gap that blocked applying a
    coupon after signup (the code was stampable only at subscribe()). Validates the coupon exists, is
    active and unexpired and within its redemption cap (fail closed, exactly like subscribe()), claims
    the redemption through the SAME atomic CAS, and is idempotent by target state (re-attaching the
    code already present claims nothing further). `tenant` guards against touching another tenant's
    subscription."""
    try:
        result = await (await build_billing(request, body.tenant)).attach_coupon(
            subscription_id=sub_id, tenant=body.tenant, coupon_code=body.coupon_code)
    except ValueError as exc:
        # DELIBERATE exception to the service's generic-error convention, on THIS internal endpoint
        # only: a machine-readable `reason` accompanies the refusal so the caller can show the shop
        # owner the REAL cause (expired vs used-up vs no-such-code vs wrong-plan) instead of a generic
        # "failed". Safe to name here — the caller is a trusted peer over the internal token that
        # already holds the coupon code, so this discloses nothing it does not have. The response is
        # built directly (not a raised HTTPException) because the global handler would collapse the
        # detail to a bare code; the security middleware still hardens it via call_next. Same
        # reasoning as the TaxProfileIncomplete handler, which names its missing key on purpose.
        msg = str(exc)
        if "SUBSCRIPTION_NOT_FOUND" in msg:
            # 404 stays NON-distinguishing: absent and another-tenant's are the SAME answer (IDOR).
            return JSONResponse({"error": "NOT_FOUND", "reason": "SUBSCRIPTION_NOT_FOUND"},
                                status_code=status.HTTP_404_NOT_FOUND)
        reason = msg if msg in _ATTACH_REASONS else "COUPON_REFUSED"   # whitelist codes we emit
        return JSONResponse({"error": "BAD_REQUEST", "reason": reason},
                            status_code=status.HTTP_400_BAD_REQUEST)
    if result.get("status") == "attached":
        # Only a real state change is witnessed; a no-op re-attach (retry) records nothing.
        await request.app.state.auditor.record(
            "subscription.coupon_attached", actor="", tenant=body.tenant,
            resource=f"subscription:{sub_id}", ip=request.client.host if request.client else "",
            metadata={"coupon_code": body.coupon_code})
    return result


class CancelInternalIn(BaseModel):
    """The internal twin of POST /api/v1/billing/subscriptions/{sub_id}/cancel. The TENANT rides in the
    body — the caller is a product service acting FOR a shop (e.g. operations cascading a cancel when a
    dependency lapses), not a human whose token carries a tenant. tenant is the IDOR guard."""
    tenant: str = Field(min_length=1, max_length=128)
    at_period_end: bool = True


@router.post("/billing/subscriptions/{sub_id}/cancel",
             dependencies=[Depends(require_internal_token)])
async def cancel_internal(request: Request, sub_id: str, body: CancelInternalIn) -> dict:
    """Cancel a shop's subscription service-to-service — the internal twin of the user cancel route,
    for a peer's cascade (e.g. a dependent app that requires visit/bookings must be REALLY cancelled,
    not just blocked, once both are gone). Same core .cancel(); at_period_end mirrors the user route
    (True = cancel at period end, False = now). tenant is REQUIRED and verified: a subscription whose
    tenant differs is reported absent (404), never cancelled or confirmed for another tenant."""
    svc = await build_billing(request, body.tenant)
    sub = await svc._subs.fetch(sub_id)
    if sub is None or sub.tenant != body.tenant:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "NOT_FOUND")
    result = (await svc.cancel(sub_id, at_period_end=body.at_period_end)).to_dict()
    await request.app.state.auditor.record(
        "subscription.cancelled", actor="", tenant=sub.tenant,
        resource=f"subscription:{sub_id}", ip=request.client.host if request.client else "",
        metadata={"at_period_end": str(body.at_period_end), "via": "internal"})
    return result


@router.post("/billing/usage/consume", dependencies=[Depends(require_internal_token)])
async def usage_consume(body: ConsumeIn, request: Request) -> dict:
    """THE enforcing call: atomic check-and-increment of a counter entitlement.

    Idempotent by `idempotency_key` — our event bus is at-least-once, and a redelivered
    consume must not burn a second unit of the tenant's quota.
    """
    return await (await build_usage(request)).consume(
        tenant=body.tenant, key=body.key, amount=body.amount,
        idempotency_key=body.idempotency_key)


@router.post("/billing/usage/release", dependencies=[Depends(require_internal_token)])
async def usage_release(body: ConsumeIn, request: Request) -> dict:
    """Give a counter unit back when the action failed. Never refused; clamped at 0."""
    return await (await build_usage(request)).release(tenant=body.tenant, key=body.key, amount=body.amount)


@router.post("/billing/usage/set", dependencies=[Depends(require_internal_token)])
async def usage_set(body: SetUsageIn, request: Request) -> dict:
    """Report a GAUGE's absolute value (identity-service reports seats).

    Absolute, not additive: a gauge that is incremented only ever grows, so removing a user
    would never give the seat back.
    """
    return await (await build_usage(request)).set_usage(tenant=body.tenant, key=body.key, value=body.value)


@router.get("/billing/usage", dependencies=[Depends(require_internal_token)])
async def usage_summary_internal(request: Request, tenant: str) -> dict:
    return await (await build_usage(request)).summary(tenant)


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
    """Billing deletes NOTHING — the invoice is a legal record. It pseudonymises the buyer.

    The financial fact survives for the tax inspector; the human does not. The receipt quotes the
    retention period back to the person, so "we kept it" is an answer with a law attached.
    """
    # Write-ahead: a critical action the log cannot take must not happen at all, so this
    # runs BEFORE the mutation. Recording afterwards would 503 a caller whose action had
    # already succeeded, and leave it unrecorded.
    await request.app.state.auditor.record(
        "subject.erased", actor="", tenant=body.tenant, resource=f"subject:{subject_ref(get_settings().PII_SALT, body.tenant, body.subject)}",
        ip=request.client.host if request.client else "")
    c = request.app.state.container
    s = get_settings()
    eraser = BillingErasure(store=c.datastore, pii_salt=s.PII_SALT,
                            retention_years=s.INVOICE_RETENTION_YEARS)
    return await eraser.erase(body.tenant, body.subject)

@router.post("/billing/grants/drain", dependencies=[Depends(require_internal_token)])
async def drain_grants(request: Request, settings: Settings = Depends(get_settings)) -> dict:
    """Deliver every credit grant that is due.

    Driven by the platform's scheduler rather than a thread in here, so a restart cannot lose the
    only thing that was retrying, and so an operator can see the schedule.
    """
    from app.api.v1.billing import build_grants
    return await build_grants(request, settings).drain(limit=settings.GRANT_DRAIN_LIMIT)


@router.post("/billing/grants/reconcile", dependencies=[Depends(require_internal_token)])
async def reconcile_grants(request: Request, settings: Settings = Depends(get_settings)) -> dict:
    """Rebuild grant rows for paid invoices that never got one.

    This is what makes the path survivable. If the process died between recording the payment and
    recording the grant, the outbox has no idea anything was owed — but the paid invoice is still
    there, and it is the durable fact. A read is self-healing; a missed step is wrong for ever.
    """
    from app.api.v1.billing import build_grants
    from app.core.stores import InvoiceStore
    return await build_grants(request, settings).reconcile(
        invoices=InvoiceStore(request.app.state.container.datastore),
        limit=settings.GRANT_RECONCILE_LIMIT)


@router.post("/billing/grants/reconcile-clawbacks", dependencies=[Depends(require_internal_token)])
async def reconcile_clawbacks(request: Request, settings: Settings = Depends(get_settings)) -> dict:
    """Rebuild clawback rows for voided invoices whose delivered grant was never reversed.

    The mirror of the grant reconcile: if the process died between voiding an invoice and recording
    the clawback, nothing in the outbox knows a reversal is owed — but the voided invoice is the
    durable fact. Only invoices whose grant was actually DELIVERED get a clawback; an undelivered
    grant is cancelled, not reversed. Drained by the same grants/drain sweep as everything else.
    """
    from app.api.v1.billing import build_grants
    from app.core.stores import InvoiceStore
    return await build_grants(request, settings).reconcile_clawbacks(
        invoices=InvoiceStore(request.app.state.container.datastore),
        limit=settings.GRANT_RECONCILE_LIMIT)


class _BackupExportIn(BaseModel):
    tenant: str = ""


class _BackupImportIn(BaseModel):
    archive: dict
    overwrite: bool = False
    # Empty = restore in place. A value REMAPS the restore into that tenant (restore-as-clone):
    # only rows that carry a tenant are reassigned; a global dataset is left alone.
    into_tenant: str = ""


def _now_iso() -> str:
    import datetime as _dt
    return _dt.datetime.now(_dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _sealer(settings: Settings):
    """The backup (seal, unseal) pair, or 503 when no backup key is configured — refusing rather
    than sealing a payment archive in the clear."""
    if not settings.BACKUP_KEK:
        raise HTTPException(
            status.HTTP_503_SERVICE_UNAVAILABLE,
            "BACKUP_KEK not configured — set this SHARED key (the SAME value backup-service holds) on this service, or its archives cannot be opened")
    return _backup.make_sealer(settings.BACKUP_KEK)


@router.post("/backup/export", dependencies=[Depends(require_internal_token)])
async def backup_export(request: Request, body: _BackupExportIn,
                        settings: Settings = Depends(get_settings)) -> dict:
    """A sealed, checksummed archive of billing's declared data — the money record included.

    Called by backup-service over the internal token. billing knows its own shape (see
    app/backup_manifest.py); the orchestrator only stores the ciphertext.
    """
    seal, _ = _sealer(settings)
    return await _backup.export(
        request.app.state.container.datastore, BACKUP,
        service=settings.SERVICE_NAME, version=settings.SERVICE_VERSION,
        taken_at=_now_iso(), seal=seal, tenant=body.tenant,
        scan_cap=settings.BACKUP_MAX_ROWS_PER_COLLECTION)


class BackupVerifyIn(BaseModel):
    archive: dict


@router.post("/backup/verify", dependencies=[Depends(require_internal_token)])
async def backup_verify(request: Request, body: BackupVerifyIn,
                        settings: Settings = Depends(get_settings)) -> dict:
    """Prove an archive restores, by test-restoring it into a throwaway namespace and comparing.

    backup-service calls this after it stores an archive (passing back the bytes it read FROM the
    store, so corruption at rest is caught too). This service is the only party that can unseal its
    own archive and knows its own schema, so verification lives here; the orchestrator only records
    the verdict. The throwaway namespace is torn down before this returns — nothing is left behind,
    and live data is never touched.
    """
    import secrets
    _, unseal = _sealer(settings)
    try:
        return await _backup.verify_restore(
            request.app.state.container.datastore, body.archive,
            unseal=unseal, token=secrets.token_hex(8))
    except _backup.BackupError as exc:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, str(exc)) from exc


@router.post("/backup/import", dependencies=[Depends(require_internal_token)])
async def backup_import(request: Request, body: _BackupImportIn,
                        settings: Settings = Depends(get_settings)) -> dict:
    """Restore a billing archive. Idempotent; leaves newer rows alone unless overwrite is set."""
    _, unseal = _sealer(settings)
    try:
        return await _backup.import_archive(
            request.app.state.container.datastore, body.archive,
            unseal=unseal, overwrite=body.overwrite, into_tenant=body.into_tenant)
    except _backup.BackupError as exc:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, str(exc)) from exc
