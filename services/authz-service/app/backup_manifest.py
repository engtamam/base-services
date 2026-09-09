"""What backup-service must capture to restore authz-service, and what it may skip.

A pure declaration, read as a literal — like admin_actions and auditable_actions. The
`safety:backup-coverage` gate checks this against the collections the code actually writes, so a
collection added later without a line here fails the build rather than being silently missing the
day a restore is needed.

EVERY COLLECTION IS ACCOUNTED FOR. A row is either backed up or excluded-with-a-reason. There is no
third state, because the third state is the hole this whole phase exists to close.

THIS IS THE AUTHORIZATION RECORD — who may do what. Lose it and every permission decision fails
open or closed depending on the default, and no one can prove who was granted what. roles and
policies are keyed by a tenant-independent random id, so they round-trip (and clone into a new
tenant) under exactly that id. assignments' key is the COMPOSITE "{tenant}::{subject}", declared
as a key TEMPLATE rather than left to the default "{id}": a restore-as-clone (into_tenant) remaps
the row's tenant, and the template rebuilds the key from the REMAPPED fields so the clone lands
under the destination tenant's key. Left as "{id}", the clone would rebuild the SOURCE tenant's
key from the unremapped id and either be skipped (clone gets no assignments) or, with overwrite,
clobber the source tenant's own row — silent cross-tenant corruption.
"""
from __future__ import annotations

from typing import Any

BACKUP: dict[str, Any] = {
    "object_prefix": "",        # authz-service writes to the datastore only — no object-store data
    "collections": [
        {"name": "roles", "tenant_scoped": True},        # role definitions (name -> capabilities)
        {"name": "policies", "tenant_scoped": True},     # allow/deny policy rules
        # subject -> roles/caps. Composite key so restore-as-clone rebuilds it from the remapped
        # tenant (see the module docstring); the row carries `tenant` and `subject` fields.
        {"name": "assignments", "tenant_scoped": True, "key": "{tenant}::{subject}"},
    ],
    "excluded": [],
}
