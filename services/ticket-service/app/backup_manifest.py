"""What backup-service must capture to restore this service, and what it may skip.

A pure declaration, read as a literal — like admin_actions and auditable_actions. The
`safety:backup-coverage` gate checks this against the collections the code actually writes, so a
collection added later without a line here fails the build rather than being silently missing the
day a restore is needed.

FILL THIS as the generated service grows a datastore. Every collection the code writes must appear
either in `collections` (backed up) or `excluded` (skipped, with a reason a person can challenge).
There is no third state — the third state is a silent hole, which is the one thing a backup system
must never have. A brand-new service that writes nothing yet declares nothing yet, and that is
correct; the gate reports it as "not onboarded" rather than failing.

`tenant_scoped` on each collection tells a restore whether it may be restored for one tenant in
isolation, or only whole. Get it wrong toward False and a per-tenant restore refuses; wrong toward
True and it may restore rows that were never tenant-partitioned.
"""
from __future__ import annotations

from typing import Any

BACKUP: dict[str, Any] = {
    "object_prefix": "",        # set if this service stores files in the object store
    "collections": [
        {"name": "tickets", "tenant_scoped": True},
        {"name": "ticket_events", "tenant_scoped": True},
        {"name": "notifications", "tenant_scoped": True},
        # The inbound-bridge indexes. Both are keyed by a length-prefixed (tenant|…) string, not a
        # tenant COLUMN, so they are restored whole (tenant_scoped: False) — the key already keeps one
        # tenant's rows from colliding with another's.
        #   inbound_threads   — the open-thread index (tenant|channel|sender -> ticket_id). Durable and
        #     restore-relevant: without it, after a restore a customer's next message opens a DUPLICATE
        #     ticket instead of appending to their still-open one.
        #   inbound_ingested  — the per-message idempotency claim (channel:external_id -> seen). Backed
        #     up so a restore does not re-ingest (duplicate-reply) messages an at-least-once source
        #     re-delivers; the backup→disaster gap is the only remaining re-delivery window.
        {"name": "inbound_threads", "tenant_scoped": False},
        {"name": "inbound_ingested", "tenant_scoped": False},
    ],
    "excluded": [
    ],
}
