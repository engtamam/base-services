"""What backup-service must capture to restore job-service, and what it may skip.

A pure declaration, read as a literal. The `safety:backup-coverage` gate checks it against the
collections the code actually writes, so a collection added later without a line here fails the build.

The durable state is the jobs themselves, the schedules that spawn them, and the due-index that says
what is claimable — all id-keyed (jobs_due carries its job id as the row id). The lease table and the
uniqueness table are transient: a lease is a worker's short-lived claim (no worker holds one after a
restore, and the reclaimer re-derives them from the jobs), and the uniqueness/idempotency table is
TTL-bounded, caller-keyed dedup state that self-heals as jobs are re-enqueued.
"""
from __future__ import annotations

from typing import Any

BACKUP: dict[str, Any] = {
    "object_prefix": "",
    "collections": [
        {"name": "jobs", "tenant_scoped": True},
        {"name": "schedules", "tenant_scoped": True},
        {"name": "jobs_due", "tenant_scoped": True},     # the claimable-index; row id = job id
    ],
    "excluded": [
        {"name": "jobs_leased",
         "reason": "in-flight worker leases (job_id -> lease_expires); transient. After a restore no "
                   "worker holds a lease, and the reclaimer re-establishes them from the jobs"},
        {"name": "jobs_unique",
         "reason": "TTL-bounded uniqueness/idempotency keys (caller-derived key -> {job_id, ts}); "
                   "expires on its own and self-heals as jobs are re-enqueued. Not durable data"},
    ],
}
