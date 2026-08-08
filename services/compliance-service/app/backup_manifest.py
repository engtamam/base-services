"""What backup-service must capture to restore compliance-service, and what it may skip.

A pure declaration, read as a literal — like admin_actions and auditable_actions. The
`safety:backup-coverage` gate checks this against the collections the code actually writes, so a
collection added later without a line here fails the build rather than being silently missing the
day a restore is needed.

EVERY COLLECTION IS ACCOUNTED FOR. A row is either backed up or excluded-with-a-reason. There is no
third state, because the third state is the hole this whole phase exists to close.

THESE ARE LEGAL RECORDS. Consent, the append-only consent proof, DSARs, legal holds, the RoPA, the
breach register, and the data map are the evidence a regulator asks for — losing them is not a
degraded service, it is the inability to prove the platform ever had a lawful basis. So all of it is
backed up. The consent row carries a stamped `id` = its canonical key (see stores.ConsentStore.put),
because the consent key normalises the purpose and is therefore not a plain template of the raw row.

THE PROOF SEQUENCE COUNTER IS DERIVED, NOT STORED TWICE. consent_proof_seq maps (tenant, subject) ->
the next proof number, and its row is just {"n": …} — it carries none of its own key, so it cannot
round-trip. It is rebuilt on import from the proofs themselves: each proof's id begins with its
zero-padded sequence, so the counter is max(seq) per (tenant, subject). Restoring the proofs and
recomputing the counter keeps the next appended proof from re-using a number — the same care the
proof's insertion-order id exists to protect.
"""
from __future__ import annotations

from typing import Any

BACKUP: dict[str, Any] = {
    "object_prefix": "",        # compliance-service writes to the datastore only — no object-store data
    "collections": [
        {"name": "consent", "tenant_scoped": True},               # id = canonical "tenant::subject::purpose"
        {"name": "consent_proof", "tenant_scoped": True},         # append-only evidence; id-keyed
        {"name": "dsar", "tenant_scoped": True},                  # data-subject requests
        {"name": "legal_holds", "tenant_scoped": True},           # holds that suspend erasure
        {"name": "processing_activities", "tenant_scoped": True},  # the RoPA
        {"name": "breaches", "tenant_scoped": True},              # breach register
        {"name": "data_targets", "tenant_scoped": True},          # the data map (who holds a subject)
    ],
    "excluded": [
        {"name": "consent_proof_seq",
         "reason": "derived counter ((tenant,subject) -> next proof number); its row is {\"n\": N} "
                   "and carries none of its own key, so it cannot round-trip. Rebuilt from the proofs "
                   "on import (max sequence per subject), so the next appended proof never re-uses a "
                   "number"},
    ],
}
