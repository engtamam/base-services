"""What backup-service must capture to restore audit-service, and what it may skip.

A pure declaration, read as a literal — like admin_actions and auditable_actions. The
`safety:backup-coverage` gate checks this against the collections the code actually writes, so a
collection added later without a line here fails the build rather than being silently missing the
day a restore is needed.

EVERY COLLECTION IS ACCOUNTED FOR. A row is either backed up or excluded-with-a-reason. There is no
third state, because the third state is the hole this whole phase exists to close.

THIS IS THE TAMPER-EVIDENT WITNESS LOG. Every event is hash-chained to the one before it, and
`audit_meta` holds the chain HEAD per partition (seq counter + head_hash). Both must survive together
and CONSISTENT: restore the events without the head and the next appended event links to the wrong
hash and its seq collides — a break in the very chain whose job is to prove nothing was broken.

BACKED UP WHOLE, not per tenant. The hash chain spans every event in a partition regardless of which
tenant it concerns, so a per-tenant slice could not be verified on its own — the log is a
platform-wide witness, restored entire. audit_meta is keyed by its partition (carried in the row by
both write paths), so it declares that key template rather than relying on a row id it does not have.
"""
from __future__ import annotations

from typing import Any

BACKUP: dict[str, Any] = {
    "object_prefix": "",        # audit-service writes to the datastore only — no object-store data
    "collections": [
        # The chain itself — id-keyed, ordered within a partition by _seq_key. NOT tenant_scoped:
        # the chain is verified whole, not per tenant.
        {"name": "audit_events", "tenant_scoped": False},
        # The chain head per partition (seq + head_hash). Keyed by partition, carried in the row.
        {"name": "audit_meta", "tenant_scoped": False, "key": "{partition}"},
    ],
    "excluded": [
        {"name": "audit_idempo",
         "reason": "ingest idempotency keys (idempotency_key -> {seq, hash, ts}) — transient dedup "
                   "state for the sink's retry window (seconds), pruned by retention. The receipt it "
                   "guards lives in audit_events; restoring stale keys would only, at worst, dedup a "
                   "post-restore retry against an event that is already present"},
    ],
}
