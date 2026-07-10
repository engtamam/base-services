# =============================================================================
# wallet-service — Internal tier (service-to-service, X-Internal-Token)
# -----------------------------------------------------------------------------
# NOT user-facing. Guarded by a shared token compared in CONSTANT TIME
# (CWE-208: observable timing discrepancy). Never a user JWT.
#
# ALL money movement lives here. A customer must never be able to credit their
# own wallet, so the user tier (/api/v1/wallet) is read-only.
# =============================================================================
from __future__ import annotations

import hmac

from fastapi import APIRouter, Depends, Header, HTTPException, Request, status
from pydantic import BaseModel, Field

from app.api.v1.wallet import build_wallet
from app.config import Settings, get_settings
from app.core import funds as _funds
from app.core.money import MoneyError
from app.core.wallet_service import Conflict, WalletError
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


class CreditIn(BaseModel):
    tenant: str = Field(min_length=1, max_length=200)
    fund: str = Field(min_length=1, max_length=64)
    amount: int = Field(ge=0)                          # integer MINOR units; never a float
    source: str = Field(default="", max_length=200)    # idempotency key
    reason: str = Field(default="", max_length=200)
    expires_at: float | None = None                    # a grant: unused remainder is clawed back


class SpendIn(BaseModel):
    tenant: str = Field(min_length=1, max_length=200)
    net: int = Field(ge=0)                             # the PRE-VAT price, in minor units
    source: str = Field(default="", max_length=200)
    reason: str = Field(default="", max_length=200)


class HoldIn(BaseModel):
    hold_id: str = Field(min_length=1, max_length=100)


class RefundIn(BaseModel):
    tenant: str = Field(min_length=1, max_length=200)
    seq: int = Field(ge=1)                             # ledger sequence of the spend to reverse
    source: str = Field(default="", max_length=200)
    reason: str = Field(default="", max_length=200)


def _handle(exc: Exception) -> HTTPException:
    """Money errors are 4xx and never leak internals; contention is a retryable 409."""
    if isinstance(exc, _funds.InsufficientFunds):
        return HTTPException(status_code=402, detail="INSUFFICIENT_FUNDS")
    if isinstance(exc, Conflict):
        return HTTPException(status_code=409, detail="WALLET_CONTENTION")
    if isinstance(exc, (WalletError, MoneyError)):
        return HTTPException(status_code=400, detail=str(exc))
    raise exc


@router.get("/wallet/{tenant}", dependencies=[Depends(require_internal_token)])
async def get_balance(tenant: str, request: Request) -> dict:
    return await build_wallet(request).balance(tenant)


@router.get("/wallet/{tenant}/reconcile", dependencies=[Depends(require_internal_token)])
async def reconcile(tenant: str, request: Request) -> dict:
    """Prove the materialized balance equals sum(ledger). Two writes, one truth."""
    return await build_wallet(request).reconcile(tenant)


@router.post("/wallet/credit", dependencies=[Depends(require_internal_token)])
async def credit(body: CreditIn, request: Request) -> dict:
    """Add money. Call this only once the PSP has SETTLED — recording an intent is not money."""
    try:
        return await build_wallet(request).credit(
            tenant=body.tenant, fund=body.fund, amount=body.amount, source=body.source,
            reason=body.reason, expires_at=body.expires_at)
    except Exception as exc:  # noqa: BLE001
        raise _handle(exc) from exc


@router.post("/wallet/spend", dependencies=[Depends(require_internal_token)])
async def spend(body: SpendIn, request: Request) -> dict:
    """Direct debit. Refuses rather than overdraw, and a refusal mutates nothing."""
    try:
        return await build_wallet(request).spend(tenant=body.tenant, net=body.net,
                                                 source=body.source, reason=body.reason)
    except Exception as exc:  # noqa: BLE001
        raise _handle(exc) from exc


@router.post("/wallet/reserve", dependencies=[Depends(require_internal_token)])
async def reserve(body: SpendIn, request: Request) -> dict:
    """Hold funds for an in-flight action. The hold EXPIRES; it cannot strand the money."""
    try:
        return await build_wallet(request).reserve(tenant=body.tenant, net=body.net,
                                                   source=body.source, reason=body.reason)
    except Exception as exc:  # noqa: BLE001
        raise _handle(exc) from exc


@router.post("/wallet/confirm", dependencies=[Depends(require_internal_token)])
async def confirm(body: HoldIn, request: Request) -> dict:
    """The action succeeded: consume the hold. Replaying this must not charge twice."""
    try:
        return await build_wallet(request).confirm(hold_id=body.hold_id)
    except Exception as exc:  # noqa: BLE001
        raise _handle(exc) from exc


@router.post("/wallet/release", dependencies=[Depends(require_internal_token)])
async def release(body: HoldIn, request: Request) -> dict:
    """The action failed: free the hold. Nothing was debited, so nothing is refunded."""
    try:
        return await build_wallet(request).release(hold_id=body.hold_id)
    except Exception as exc:  # noqa: BLE001
        raise _handle(exc) from exc


@router.post("/wallet/refund", dependencies=[Depends(require_internal_token)])
async def refund(body: RefundIn, request: Request) -> dict:
    """Reverse a spend INTO THE FUNDS IT CAME FROM (a refunded gift stays a gift)."""
    try:
        return await build_wallet(request).refund(tenant=body.tenant, seq=body.seq,
                                                  source=body.source, reason=body.reason)
    except Exception as exc:  # noqa: BLE001
        raise _handle(exc) from exc


@router.post("/wallet/sweep-holds", dependencies=[Depends(require_internal_token)])
async def sweep_holds(request: Request) -> dict:
    """Release abandoned holds. Without this a dead client locks its credits forever."""
    return await build_wallet(request).sweep_holds()


@router.post("/wallet/sweep-grants", dependencies=[Depends(require_internal_token)])
async def sweep_grants(request: Request) -> dict:
    """Claw back the unused remainder of expired grants. The customer keeps what they spent."""
    return await build_wallet(request).sweep_grants()


@router.get("/health", dependencies=[Depends(require_internal_token)])
async def internal_health(settings: Settings = Depends(get_settings)) -> dict[str, str]:
    return {"status": "ok", "service": settings.SERVICE_NAME}


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
