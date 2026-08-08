"""What backup-service must capture to restore tenant-service, and what it may skip.

A pure declaration, read as a literal — like admin_actions and auditable_actions. The
`safety:backup-coverage` gate checks this against the collections the code actually writes, so a
collection added later without a line here fails the build rather than being silently missing the
day a restore is needed.

EVERY COLLECTION IS ACCOUNTED FOR. A row is either backed up or excluded-with-a-reason. There is no
third state, because the third state is the hole this whole phase exists to close.

THIS IS THE TENANT REGISTRY — the root of every other service's tenancy. Lose `tenants` and every
tenant-scoped row elsewhere is orphaned data no one can attribute to a customer, so it is backed up
whole (it is NOT tenant_scoped: the registry maps ALL tenants; a per-tenant export of tenant-service
is meaningless — a tenant cannot back up the registry it lives in). The audit trail is NOT here — it
is written to audit-service, which backs up its own store.

THE TWO INDEXES ARE DERIVED, NOT DATA. `tenant_slugs` (slug -> {id}) and `tenant_domains`
(host -> {slug}) are keyed by slug/host, but their VALUES do not echo that key — the backup core
keys a restore on each row's own `id`, which these rows do not carry as their datastore key. They
are excluded from the archive and REBUILT from `tenants` on import (see internal.py's reindex): a
restore of the registry reconstructs both lookups exactly, so nothing is lost. Backing them up
verbatim would restore them under the wrong keys — worse than not backing them up at all.
"""
from __future__ import annotations

from typing import Any

BACKUP: dict[str, Any] = {
    "object_prefix": "",        # tenant-service writes to the datastore only — no object-store data
    "collections": [
        {"name": "tenants", "tenant_scoped": False},        # the registry — one row per tenant, the source of truth
    ],
    "excluded": [
        {"name": "tenant_slugs",
         "reason": "derived index (slug -> tenant id); keyed by slug, not by row id, so it cannot "
                   "round-trip through the id-keyed backup core. Rebuilt from `tenants` on restore "
                   "(internal.py reindex) — the data is fully recovered, just not stored twice"},
        {"name": "tenant_domains",
         "reason": "derived index (custom host -> slug); same as tenant_slugs — keyed by host, "
                   "value carries no id. Rebuilt from each tenant's `domains` on restore"},
    ],
}
