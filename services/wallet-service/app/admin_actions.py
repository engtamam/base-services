"""wallet-service — admin actions: read-only, by design.

Money only ever MOVES through internal-token, service-to-service routes (credit / spend / reserve /
confirm / release / refund / sweep-*). There is deliberately NO user-capability route that mutates a
balance, so an operator can never mint or burn funds from the panel — the panel can SHOW the money
but never move it. That constraint is a feature, not a gap: it is why a panel breach cannot become a
theft.

What the panel CAN do is what support and ops actually need — read a SPECIFIC tenant's wallet. The
self-service reads (a tenant reading its own balance from its token) are not operator actions; these
are the cross-tenant operator reads, gated by platform.wallet ONLY (never wallet.read/manage, so a
tenant user can never name another tenant), with the tenant an explicit, audited argument. Every one
is a read — a money projection, never the subject's PII — and every access is recorded.
"""
from __future__ import annotations

from typing import Any


ADMIN_ACTIONS: list[dict[str, Any]] = [
    {
        "id": "wallet.balance", "label": "View a tenant's balance", "method": "GET",
        "path": "/api/v1/wallet/admin/balance", "capability": "platform.wallet", "group": "Wallet",
        "params": [{"name": "tenant", "in": "query", "type": "string", "required": True,
                    "help": "the tenant whose wallet to inspect"}],
    },
    {
        "id": "wallet.ledger", "label": "View a tenant's statement", "method": "GET",
        "path": "/api/v1/wallet/admin/ledger", "capability": "platform.wallet", "group": "Wallet",
        "params": [
            {"name": "tenant", "in": "query", "type": "string", "required": True,
             "help": "the tenant whose statement to inspect"},
            {"name": "limit", "in": "query", "type": "integer", "required": False,
             "help": "page size (clamped by the service)"},
            {"name": "before_seq", "in": "query", "type": "integer", "required": False,
             "help": "cursor: return entries before this ledger sequence"},
        ],
    },
    {
        "id": "wallet.reconcile", "label": "Reconcile a tenant's wallet", "method": "GET",
        "path": "/api/v1/wallet/admin/reconcile", "capability": "platform.wallet", "group": "Wallet",
        "params": [{"name": "tenant", "in": "query", "type": "string", "required": True,
                    "help": "the tenant whose wallet to reconcile"}],
        # A read-only integrity check: does sum(ledger) still equal the materialized balance? The
        # support answer to "the numbers look wrong" — surfaced without moving a cent.
    },
]
