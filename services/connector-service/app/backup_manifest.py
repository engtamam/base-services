"""What backup-service must capture to restore connector-service, and what it may skip.

A pure declaration, read as a literal. The `safety:backup-coverage` gate checks it against the
collections the code actually writes, so a collection added later without a line here fails the build.

The durable data is the integration configuration and its history — connections, provider specs, sync
runs, and the dead-letter queue of records that failed to sync (worth keeping: they are pending work,
not noise). All are id-keyed. The per-record idempotency set is excluded — it regenerates from real
sync traffic and restoring stale keys would at worst suppress one legitimate re-sync.
"""
from __future__ import annotations

from typing import Any

BACKUP: dict[str, Any] = {
    "object_prefix": "",
    "collections": [
        {"name": "connections", "tenant_scoped": True},
        {"name": "provider_specs", "tenant_scoped": True},
        {"name": "sync_runs", "tenant_scoped": True},
        {"name": "sync_dlq", "tenant_scoped": True},     # failed records awaiting retry — pending work
    ],
    "excluded": [
        {"name": "sync_seen",
         "reason": "per-record idempotency keys (key -> {k}); regenerated from sync traffic, not data. "
                   "Restoring stale keys would at worst suppress a legitimate re-sync, never lose data"},
    ],
}
