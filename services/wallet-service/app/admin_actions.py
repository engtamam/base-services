"""wallet-service — admin actions: read-only, by design.

Money only ever MOVES through internal-token, service-to-service routes (credit / spend / reserve /
confirm / release / refund / sweep-*). There is deliberately NO user-capability route that mutates a
balance, so an operator can never mint or burn funds from the panel — the panel can SHOW the money
but never move it. The two user routes are reads gated by platform.wallet / wallet.read; both are
tenant-scoped from the token. Every action re-checks its capability at the route.
"""
from __future__ import annotations

from typing import Any


ADMIN_ACTIONS: list[dict[str, Any]] = [
    {
        "id": "wallet.balance", "label": "View wallet balance", "method": "GET",
        "path": "/api/v1/wallet", "capability": "platform.wallet", "group": "Wallet",
    },
    {
        "id": "wallet.ledger", "label": "View wallet ledger", "method": "GET",
        "path": "/api/v1/wallet/ledger", "capability": "platform.wallet", "group": "Wallet",
    },
]
