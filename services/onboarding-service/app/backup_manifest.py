"""What backup-service must capture to restore onboarding-service, and what it may skip.

A pure declaration, read as a literal. The `safety:backup-coverage` gate checks it against the
collections the code actually writes, so a collection added later without a line here fails the build.

All four collections are id-keyed (every row carries its own id as the datastore key). The grant
ledger and the grants table are the ANTI-ABUSE record of who has already collected a one-time
onboarding grant — lose them and a customer could claim a welcome credit twice. onboarding_forced is
the operator's manual override of a requirement, and provisioning_runs is the tenant-provisioning
saga state, which a resumed rollback reads. All durable, all restored under their own id.
"""
from __future__ import annotations

from typing import Any

BACKUP: dict[str, Any] = {
    "object_prefix": "",
    "collections": [
        {"name": "grant_ledger", "tenant_scoped": True},
        {"name": "onboarding_grants", "tenant_scoped": True},
        {"name": "onboarding_forced", "tenant_scoped": True},    # id = "tenant::subject::requirement"
        {"name": "provisioning_runs", "tenant_scoped": True},    # the provisioning saga state
    ],
    "excluded": [
        {"name": "provisioning_locks",
         "reason": "a short-lived advisory MUTEX (run_key -> {at}) taken before a provisioning saga "
                   "and deleted in its finally — transient. After a restore no saga is running, so a "
                   "restored stale lock would only BLOCK a legitimate run; the clean slate is correct. "
                   "The durable saga state lives in provisioning_runs, which IS backed up"},
    ],
}
