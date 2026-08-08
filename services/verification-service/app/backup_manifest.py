"""What backup-service must capture to restore verification-service, and what it may skip.

A pure declaration, read as a literal. The `safety:backup-coverage` gate checks it against the
collections the code actually writes, so a collection added later without a line here fails the build.

EVERY COLLECTION IS ACCOUNTED FOR. Both collections are id-keyed (datastore key == row id), so each
restores under its own key with the default template.

identity_seen is NOT mere idempotency — it is the "once per identity" ledger an onboarding grant
checks so the same person cannot re-collect a one-time welcome grant. Losing it would let a verified
identity claim its grant again, so it is backed up, not excluded. It is keyed by a claim pseudonym
with no tenant field, hence tenant_scoped=False (a per-tenant export returns it whole).
"""
from __future__ import annotations

from typing import Any

BACKUP: dict[str, Any] = {
    "object_prefix": "",
    "collections": [
        {"name": "verifications", "tenant_scoped": True},      # the verification records; id-keyed
        {"name": "identity_seen", "tenant_scoped": False},     # once-per-identity ledger; id = claim ref
    ],
    "excluded": [],
}
