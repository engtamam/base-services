"""Security MEDIUM remediation: no-tenant wallet request refused (wallet-005) and settle_withdrawal
audit tenant (wallet-008)."""
from __future__ import annotations

import pytest
from fastapi import HTTPException


# ── wallet-005: a user-tier caller with no tenant is refused, not bucketed ────────
def test_no_tenant_is_refused():
    from app.api.auth import Principal
    from app.api.v1.wallet import _tenant
    with pytest.raises(HTTPException) as ei:
        _tenant(Principal(subject="u", caps=frozenset(), tenant=None))
    assert ei.value.status_code == 403


def test_tenant_is_returned_when_present():
    from app.api.auth import Principal
    from app.api.v1.wallet import _tenant
    assert _tenant(Principal(subject="u", caps=frozenset(), tenant="acme")) == "acme"


# ── wallet-008: settle_withdrawal result carries the tenant (so the audit can name it) ──
def test_settle_result_includes_tenant():
    import asyncio
    from app.core.wallet_service import WalletService
    from app.core.models import WITHDRAW_PAID, WITHDRAW_PENDING, Withdrawal

    wd = Withdrawal(id="wd1", tenant="shop_a", applied={"main": 100}, state=WITHDRAW_PENDING)

    class _Store:
        def __init__(self):
            self.rows = {("withdrawals", "wd1"): wd.to_dict()}
        async def get(self, coll, key):
            return self.rows.get((coll, key))
        async def put(self, coll, key, val):
            self.rows[(coll, key)] = val

    class _Clock:
        def now(self):
            return 1_700_000_000.0

    svc = WalletService.__new__(WalletService)
    svc._store = _Store()
    svc._clock = _Clock()

    async def _claim(tenant, op, wid):
        return True, None

    async def _finalize(tenant, op, wid, result):
        return None

    svc._claim = _claim
    svc._finalize = _finalize
    res = asyncio.run(svc.settle_withdrawal(withdrawal_id="wd1", paid=True))
    assert res["state"] == WITHDRAW_PAID and res["tenant"] == "shop_a"
