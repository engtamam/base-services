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
        # Money records: a PaymentRequest is the authoritative "shop asked customer to pay X for
        # order R, and here is its outcome". Tenant-partitioned (every row carries `tenant`).
        {"name": "payment_requests", "tenant_scoped": True},
        # Each shop's OWN sealed gateway credentials (MERCHANT_*). Backed up SEALED (ciphertext under
        # SECRETS_KEK) so a restore keeps a shop able to take payments without re-entering keys; the
        # KEK itself is env, never in the archive, so an archive alone yields nothing. Tenant-scoped.
        {"name": "merchant_secrets", "tenant_scoped": True},
    ],
    "excluded": [
        # Idempotency/claim keys for create_link — regenerated on demand, not data worth restoring;
        # restoring stale claims could even wrongly block a re-attempt. Skipped, like dedupe keys.
        {"name": "payment_claims", "reason": "atomic (tenant,reference) idempotency claims — "
                                             "ephemeral, regenerated, not restorable state"},
    ],
}
