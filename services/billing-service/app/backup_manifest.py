"""What backup-service must capture to restore this service, and what it may skip.

A pure declaration, read as a literal — like admin_actions and auditable_actions. The
`safety:backup-coverage` gate checks this against the collections the code actually writes, so a
collection added later without a line here fails the build rather than being silently missing the
day a restore is needed.

EVERY COLLECTION IS ACCOUNTED FOR. A row is either backed up or excluded-with-a-reason. There is no
third state, because the third state is the hole this whole phase exists to close.

THE SEQUENCE COUNTERS ARE NOT OPTIONAL. `billing_seq` issues invoice numbers and `invoice_settled`
counts settled amounts. Restoring the invoices without them would re-issue numbers that already
exist — a duplicate tax-invoice number is a compliance defect, not a glitch — so they are backed up
with the same care as the invoices themselves.
"""
from __future__ import annotations

from typing import Any

BACKUP: dict[str, Any] = {
    # No object-store data: billing writes to the datastore only. A service that stored files (a
    # PDF archive, say) would name its prefix here.
    "object_prefix": "",
    "collections": [
        {"name": "plans", "tenant_scoped": False},          # the global catalogue
        {"name": "coupons", "tenant_scoped": False},        # global campaigns
        {"name": "subscriptions", "tenant_scoped": True},
        {"name": "invoices", "tenant_scoped": True},
        {"name": "payments", "tenant_scoped": True},
        {"name": "settlements", "tenant_scoped": True},
        {"name": "credit_grants", "tenant_scoped": True},   # the durable "they paid, deliver it" outbox
        {"name": "ledger", "tenant_scoped": True},          # the money record — append-only, must survive
        {"name": "ledger_balances", "tenant_scoped": True},
        {"name": "usage", "tenant_scoped": True},
        {"name": "usage_counters", "tenant_scoped": True},
        # Counters that MUST travel with the data they number, or a restore re-issues identifiers
        # that already exist in the world.
        {"name": "billing_seq", "tenant_scoped": True},     # invoice-number sequence
        {"name": "invoice_settled", "tenant_scoped": False},  # atomic settled-amount counter, keyed by invoice
        {"name": "settlement_index", "tenant_scoped": False},
    ],
    "excluded": [
        {"name": "idempo",
         "reason": "idempotency keys — regenerated on the next request; restoring stale ones would "
                   "at worst suppress a legitimate retry, never recover data"},
        {"name": "usage_idempo",
         "reason": "idempotency keys for usage recording; same as idempo — ephemeral, not data"},
    ],
}
