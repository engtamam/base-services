"""What backup-service must capture to restore authz-service, and what it may skip.

A pure declaration, read as a literal — like admin_actions and auditable_actions. The
`safety:backup-coverage` gate checks this against the collections the code actually writes, so a
collection added later without a line here fails the build rather than being silently missing the
day a restore is needed.

EVERY COLLECTION IS ACCOUNTED FOR. A row is either backed up or excluded-with-a-reason. There is no
third state, because the third state is the hole this whole phase exists to close.

THIS IS THE AUTHORIZATION RECORD — who may do what. Lose it and every permission decision fails
open or closed depending on the default, and no one can prove who was granted what. All three
collections are durable and their datastore key IS row["id"] (assignments' id is the composite
"tenant::subject", the same key the store uses), so each restores under exactly its own key.
"""
from __future__ import annotations

from typing import Any

BACKUP: dict[str, Any] = {
    "object_prefix": "",        # authz-service writes to the datastore only — no object-store data
    "collections": [
        {"name": "roles", "tenant_scoped": True},        # role definitions (name -> capabilities)
        {"name": "policies", "tenant_scoped": True},     # allow/deny policy rules
        {"name": "assignments", "tenant_scoped": True},  # subject -> roles/caps; id = "tenant::subject"
    ],
    "excluded": [],
}
