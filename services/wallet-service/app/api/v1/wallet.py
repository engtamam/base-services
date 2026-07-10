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
from app.api.authz import require_any_capability
from app.api.ratelimit import rate_limit
from app.config import get_settings
from app.core.wallet_service import WalletService

router = APIRouter(prefix="/api/v1/wallet", tags=["wallet"])

_PLATFORM = "platform.wallet"
_READ = require_any_capability(_PLATFORM, "wallet.manage", "wallet.read")


def wallet_config() -> dict[str, Any]:
    s = get_settings()
    return {"funds": s.wallet_funds, "vat_bp": s.WALLET_VAT_BP, "currency": s.WALLET_CURRENCY,
            "hold_ttl": s.HOLD_TTL_SECONDS, "hold_sweep_limit": s.HOLD_SWEEP_LIMIT,
            "grant_sweep_limit": s.GRANT_SWEEP_LIMIT,
            "idempotency_ttl": s.WALLET_IDEMPOTENCY_TTL_SECONDS,
            "ledger_list_limit": s.LEDGER_LIST_LIMIT, "cas_max_retries": s.CAS_MAX_RETRIES,
            "cas_backoff_ms": s.CAS_BACKOFF_MS}


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
