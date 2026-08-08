"""What backup-service must capture to restore zatca-service, and what it may skip.

A pure declaration, read as a literal. The `safety:backup-coverage` gate checks it against the
collections the code actually writes, so a collection added later without a line here fails the build.

THESE ARE TAX RECORDS. The ZATCA e-invoices and the per-tenant onboarding settings (which hold the
SEALED CSID certificate and private key obtained from ZATCA — re-obtaining them means re-onboarding
with the Authority) are the two things that must survive. Both are id-keyed: invoices by their own
id, and settings by the tenant — the settings row now carries its tenant as `id` (see
SettingsStore.put/store_csid) so it restores under exactly its key.

zatca_index is EXCLUDED and rebuilt on import. It mixes TWO key schemes in one collection —
"head::<tenant>" (the ICV hash-chain head: the reserved counter + last signed hash) and
"num::<tenant>::<number>" (a number→id lookup) — so no single key template fits, and its rows carry
none of their own key. It is a pure derivative of the invoices: every reserved ICV has a saved
invoice (allocate_slot persists before it reserves), so the head's reserved_icv is exactly the max
ICV per tenant and its last_hash is the hash of the highest SIGNED invoice. Rebuilding it from the
invoices on import keeps the next allocated ICV from colliding — a duplicate ICV is a compliance
defect, so this is done with the same care as the invoices themselves.
"""
from __future__ import annotations

from typing import Any

BACKUP: dict[str, Any] = {
    "object_prefix": "",
    "collections": [
        {"name": "zatca_invoices", "tenant_scoped": True},
        {"name": "zatca_settings", "tenant_scoped": True},   # id = tenant; holds the sealed CSID creds
    ],
    "excluded": [
        {"name": "zatca_index",
         "reason": "the ICV hash-chain head (head::<tenant> -> {reserved_icv, last_hash}) and the "
                   "number lookup (num::<tenant>::<number> -> {id}) in one collection — two key "
                   "schemes, rows carrying no key of their own. A pure derivative of the invoices "
                   "(every reserved ICV has a saved invoice), rebuilt from them on import so the "
                   "next allocated ICV never collides"},
    ],
}
