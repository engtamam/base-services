"""What backup-service must capture to restore wallet-service, and what it may skip.

A pure declaration, read as a literal — like admin_actions and auditable_actions. The
`safety:backup-coverage` gate checks this against the collections the code actually writes, so a
collection added later without a line here fails the build rather than being silently missing the
day a restore is needed.

EVERY COLLECTION IS ACCOUNTED FOR. A row is either backed up or excluded-with-a-reason. There is no
third state, because the third state is the hole this whole phase exists to close.

THIS IS MONEY. The ledger is the truth and the wallet row is a projection of it; both must survive,
and they must survive CONSISTENT — reconcile() proves a restored wallet still equals the sum of its
ledger. That is why the sequence counter matters: the wallet row carries `seq`, the next ledger
number for the tenant. Restore the ledger without the wallet's seq and the next spend re-issues a
number that already exists — a duplicated ledger entry is a corrupted money history, not a glitch.

THE KEY TEMPLATES ARE LOAD-BEARING. Two collections are keyed by something other than a row id, so
their backup_manifest entry declares how to rebuild the datastore key from the row's own fields
(core/backup.py honours `key`):
  * wallets      — one row per tenant, keyed "{tenant}"; the row has no id.
  * ledger       — keyed "{tenant}:{seq:012d}" (the SAME form _ledger_key writes); the row also
                   carries a business id (led_…) used as a refund ref, so its id is NOT its key.
Getting these wrong would restore a ledger entry under its led_ id and silently lose the ordered,
seq-keyed history reconcile() walks. holds and grants are keyed by their own id (the default).
"""
from __future__ import annotations

from typing import Any

BACKUP: dict[str, Any] = {
    "object_prefix": "",        # wallet-service writes to the datastore only — no object-store data
    "collections": [
        # The projection AND its sequence counter — restored under the tenant key, seq intact.
        {"name": "wallets", "tenant_scoped": True, "key": "{tenant}"},
        # The money record — immutable, ordered, keyed by (tenant, seq). The truth reconcile() walks.
        {"name": "ledger", "tenant_scoped": True, "key": "{tenant}:{seq:012d}"},
        {"name": "holds", "tenant_scoped": True},    # reserved-but-not-spent; id-keyed
        {"name": "grants", "tenant_scoped": True},   # expiring credits; id-keyed
        # Payouts. MONEY leaving the platform: a pending one is a debit awaiting an external transfer,
        # and losing it on restore would either strand the customer's debited funds or re-pay a
        # settled withdrawal. id-keyed.
        {"name": "withdrawals", "tenant_scoped": True},
        # The DURABLE once-only credit marker (audit wallet-r2-03): one row per settled top-up source,
        # keyed "{tenant}|{source}" == its own id, so the default {id} template round-trips it. This is
        # money-integrity state, NOT a throwaway idempotency key: lose it on restore and a settlement
        # webhook redelivered after the restore would double-credit, so it MUST survive. id-keyed.
        {"name": "wallet_credit_seen", "tenant_scoped": True},
        # The DURABLE once-only debit marker (audit wallet-r2-03), symmetric to wallet_credit_seen:
        # one row per spent source, keyed "{tenant}|{source}" == its own id. Money-integrity state —
        # lose it on restore and a spend replayed after the restore would double-charge. id-keyed.
        {"name": "wallet_spend_seen", "tenant_scoped": True},
    ],
    "excluded": [
        {"name": "wallet_idempo",
         "reason": "idempotency keys (tenant|op|source) — regenerated on the next request; restoring "
                   "stale ones would at worst suppress a legitimate retry, never recover money"},
        {"name": "wallet_alerts",
         "reason": "low-balance armed/triggered state, one row per tenant — a transient notification "
                   "latch, not money. It re-arms itself from the live balance on the next spend, so "
                   "restoring a stale latch would at most warn once too many or too few"},
    ],
}
