"""wallet-service — end-user (self-service) actions: a customer's OWN wallet, read-only.

The two user-tier reads (gated by wallet.read): view my balance and my ledger, scoped to the caller's
tenant from the token. The wallet is READ-ONLY for users by construction — money moves ONLY through
internal, service-to-service routes (credit/spend/refund), so a customer can never mint or move their
own funds from the app. `wallet.read` is the advertised self-service capability. (The same two reads
also appear in admin_actions under platform.wallet for an operator; here they are the customer's own.)
"""
from __future__ import annotations

from typing import Any


USER_ACTIONS: list[dict[str, Any]] = [
    {"id": "wallet.my_balance", "label": "My balance", "method": "GET",
     "path": "/api/v1/wallet", "capability": "wallet.read", "group": "Wallet"},
    {"id": "wallet.my_ledger", "label": "My transactions", "method": "GET",
     "path": "/api/v1/wallet/ledger", "capability": "wallet.read", "group": "Wallet"},
]
