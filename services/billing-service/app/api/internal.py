# =============================================================================
# billing-service — Internal tier (service-to-service, X-Internal-Token)
# -----------------------------------------------------------------------------
# NOT user-facing. Guarded by a shared token compared in CONSTANT TIME
# (CWE-208: observable timing discrepancy). Never a user JWT.
# =============================================================================
from __future__ import annotations

import hmac

from fastapi import APIRouter, Depends, Header, HTTPException, Request, status
from pydantic import BaseModel, Field

from app.api.v1.billing import build_billing, build_usage
from app.config import Settings, get_settings
from app.core.erasure import BillingErasure
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


@router.post("/billing/webhook/{provider}")
async def payment_webhook(provider: str, request: Request,
                          settings: Settings = Depends(get_settings)) -> dict:
    """Gateway webhook — signature-verified inside handle_webhook (no token gate; the
    signature IS the auth). Reconciles the payment/invoice idempotently."""
    body = await request.body()
    headers = {k.lower(): v for k, v in request.headers.items()}
    result = await build_billing(request).handle_webhook(
        provider, headers, body, settings.PAYMENT_WEBHOOK_SECRET)
    if not result.get("ok"):
        raise HTTPException(status.HTTP_400_BAD_REQUEST, result.get("reason", "INVALID"))
    return result


@router.post("/billing/dunning", dependencies=[Depends(require_internal_token)])
async def run_dunning(request: Request) -> dict:
    return await build_billing(request).dunning_sweep()


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
    return await build_usage(request).check(tenant=tenant, key=key, amount=amount)


@router.post("/billing/usage/consume", dependencies=[Depends(require_internal_token)])
async def usage_consume(body: ConsumeIn, request: Request) -> dict:
    """THE enforcing call: atomic check-and-increment of a counter entitlement.

    Idempotent by `idempotency_key` — our event bus is at-least-once, and a redelivered
    consume must not burn a second unit of the tenant's quota.
    """
    return await build_usage(request).consume(
        tenant=body.tenant, key=body.key, amount=body.amount,
        idempotency_key=body.idempotency_key)


@router.post("/billing/usage/release", dependencies=[Depends(require_internal_token)])
async def usage_release(body: ConsumeIn, request: Request) -> dict:
    """Give a counter unit back when the action failed. Never refused; clamped at 0."""
    return await build_usage(request).release(tenant=body.tenant, key=body.key, amount=body.amount)


@router.post("/billing/usage/set", dependencies=[Depends(require_internal_token)])
async def usage_set(body: SetUsageIn, request: Request) -> dict:
    """Report a GAUGE's absolute value (identity-service reports seats).

    Absolute, not additive: a gauge that is incremented only ever grows, so removing a user
    would never give the seat back.
    """
    return await build_usage(request).set_usage(tenant=body.tenant, key=body.key, value=body.value)


@router.get("/billing/usage", dependencies=[Depends(require_internal_token)])
async def usage_summary_internal(request: Request, tenant: str) -> dict:
    return await build_usage(request).summary(tenant)


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
    c = request.app.state.container
    s = get_settings()
    eraser = BillingErasure(store=c.datastore, pii_salt=s.PII_SALT,
                            retention_years=s.INVOICE_RETENTION_YEARS)
    return await eraser.erase(body.tenant, body.subject)
