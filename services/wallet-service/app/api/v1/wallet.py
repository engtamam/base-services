# =============================================================================
# wallet-service — user tier: a tenant reads its OWN balance and statement.
# -----------------------------------------------------------------------------
# Money never moves here. Every mutation is service-to-service on the internal
# tier: a customer must not be able to credit their own wallet.
# =============================================================================
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, Query, Request

from app.api.auth import Principal
from app.api.authz import require_any_capability, require_capability
from app.api.ratelimit import rate_limit
from app.config import get_settings
from app.core.pii import subject_ref
from app.core.wallet_service import WalletService

router = APIRouter(prefix="/api/v1/wallet", tags=["wallet"])

_PLATFORM = "platform.wallet"
_READ = require_any_capability(_PLATFORM, "wallet.manage", "wallet.read")
# The cross-tenant OPERATOR reads are platform-only — never wallet.read/manage. A tenant user who can
# read its OWN wallet must not, by that capability, be able to name and read another tenant's. This
# is the one wall between every company on this platform, so the operator surface is a distinct
# capability on distinct routes, and the tenant is an explicit argument that is always audited.
_OPERATOR = require_capability(_PLATFORM)


def wallet_config() -> dict[str, Any]:
    s = get_settings()
    return {"funds": s.wallet_funds, "vat_bp": s.WALLET_VAT_BP, "currency": s.WALLET_CURRENCY,
            "hold_ttl": s.HOLD_TTL_SECONDS, "hold_sweep_limit": s.HOLD_SWEEP_LIMIT,
            "grant_sweep_limit": s.GRANT_SWEEP_LIMIT,
            "idempotency_ttl": s.WALLET_IDEMPOTENCY_TTL_SECONDS,
            "withdraw_min": s.WALLET_WITHDRAW_MIN_MINOR,
            "ledger_list_limit": s.LEDGER_LIST_LIMIT, "cas_max_retries": s.CAS_MAX_RETRIES,
            "cas_backoff_ms": s.CAS_BACKOFF_MS,
            "low_balance_threshold": s.WALLET_LOW_BALANCE_THRESHOLD,
            "low_balance_mode": s.WALLET_LOW_BALANCE_MODE,
            "low_balance_fund": s.WALLET_LOW_BALANCE_FUND,
            "event_publish_failure": s.EVENT_PUBLISH_FAILURE}


def build_wallet(request: Request) -> WalletService:
    c = request.app.state.container
    return WalletService(store=c.datastore, clock=c.clock, config=wallet_config(),
                         event_bus=c.event_bus)


def _tenant(p: Principal) -> str:
    """Pinned to the token. A tenant id is never taken from the request."""
    return p.tenant or "__none__"


@router.get("", dependencies=[Depends(rate_limit)])
async def my_balance(request: Request, p: Principal = Depends(_READ)) -> dict:
    return await build_wallet(request).balance(_tenant(p))


@router.get("/ledger", dependencies=[Depends(rate_limit)])
async def my_ledger(request: Request, p: Principal = Depends(_READ),
                    limit: int = Query(default=50, ge=1, le=500),
                    before_seq: int | None = Query(default=None, ge=1)) -> dict:
    """Paginated. The service clamps `limit` to LEDGER_LIST_LIMIT regardless of what is asked."""
    rows = await build_wallet(request).ledger(_tenant(p), limit=limit, before_seq=before_seq)
    return {"entries": rows, "count": len(rows),
            "next_before_seq": rows[-1]["seq"] if rows else None}


# ── operator: read ANY tenant's wallet (support / ops), never move money ───────
def _actor(p: Principal) -> str:
    """A pseudonym for the operator. An audit row naming a person is itself personal data; the
    linkage lives only in identity-service, so this stays erasable while the trail stays useful."""
    salt = get_settings().PII_SALT
    return subject_ref(salt, p.tenant or "", p.subject) if salt else ""


async def _audit_inspect(request: Request, p: Principal, tenant: str, view: str) -> None:
    """Record the cross-tenant read BEFORE returning it. A platform operator reading a customer's
    money is exactly the access a review asks about — who looked at whose wallet, and when."""
    await request.app.state.auditor.record(
        "wallet.inspected", actor=_actor(p), tenant=tenant, resource="wallet",
        ip=request.client.host if request.client else "", metadata={"view": view})


@router.get("/admin/balance", dependencies=[Depends(rate_limit)])
async def operator_balance(request: Request, tenant: str = Query(..., min_length=1, max_length=128),
                           p: Principal = Depends(_OPERATOR)) -> dict:
    """A specific tenant's balance, for support/ops. Read-only — money never moves here; it is a
    money projection, not the subject's PII. `tenant` is mandatory and explicit, and audited."""
    await _audit_inspect(request, p, tenant, "balance")
    return await build_wallet(request).balance(tenant)


@router.get("/admin/ledger", dependencies=[Depends(rate_limit)])
async def operator_ledger(request: Request, tenant: str = Query(..., min_length=1, max_length=128),
                          limit: int = Query(default=50, ge=1, le=500),
                          before_seq: int | None = Query(default=None, ge=1),
                          p: Principal = Depends(_OPERATOR)) -> dict:
    """A specific tenant's statement, paginated (the service clamps `limit`). Audited."""
    await _audit_inspect(request, p, tenant, "ledger")
    rows = await build_wallet(request).ledger(tenant, limit=limit, before_seq=before_seq)
    return {"tenant": tenant, "entries": rows, "count": len(rows),
            "next_before_seq": rows[-1]["seq"] if rows else None}


@router.get("/admin/reconcile", dependencies=[Depends(rate_limit)])
async def operator_reconcile(request: Request, tenant: str = Query(..., min_length=1, max_length=128),
                             p: Principal = Depends(_OPERATOR)) -> dict:
    """Prove a tenant's wallet is internally consistent: sum(ledger) == the materialized balance.
    A read-only integrity check for support — it computes and compares, it does not mutate. Audited."""
    await _audit_inspect(request, p, tenant, "reconcile")
    return await build_wallet(request).reconcile(tenant)
