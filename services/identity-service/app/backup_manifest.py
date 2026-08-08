"""What backup-service must capture to restore identity-service, and what it may skip.

A pure declaration, read as a literal — like admin_actions and auditable_actions. The
`safety:backup-coverage` gate checks this against the collections the code actually writes, so a
collection added later without a line here fails the build rather than being silently missing the
day a restore is needed.

EVERY COLLECTION IS ACCOUNTED FOR. A row is either backed up or excluded-with-a-reason. There is no
third state, because the third state is the hole this whole phase exists to close.

THE INVARIANT THAT DECIDES backed-up vs derived: the backup core keys a restore on each row's own
`id`, and writes it back under that id. A collection is safe to archive ONLY when its datastore key
IS `row["id"]` — true for users, persons, memberships (id = "person::tenant"), invites (id = token
digest), operator_secrets (id = "tenant|name", sealed ciphertext), and runtime_config (id =
"tenant|key"). The two lookup indexes are keyed by a normalized identifier whose value carries only
the TARGET id, not the key — archiving them verbatim would restore them under the wrong key, so they
are EXCLUDED and REBUILT from their source collection on import (see internal.py reindex).

The credentials ARE the data here — password hashes, MFA secrets, sealed operator secrets. They are
already at rest in the store and travel in the archive exactly as they sit, sealed a second time by
the backup key. Losing them is losing every account, so they are backed up with the most care.
"""
from __future__ import annotations

from typing import Any

BACKUP: dict[str, Any] = {
    "object_prefix": "",        # identity-service writes to the datastore only — no object-store data
    "collections": [
        {"name": "users", "tenant_scoped": True},            # accounts: id, hash, MFA — the core credential
        {"name": "persons", "tenant_scoped": False},         # cross-tenant identities (multi-tenant mode)
        {"name": "memberships", "tenant_scoped": True},      # person x tenant x roles — all authority; id = "person::tenant"
        {"name": "invites", "tenant_scoped": True},          # single-use invites, token stored hashed; id = digest
        {"name": "operator_secrets", "tenant_scoped": True},  # sealed per-tenant operator secrets; id = "tenant|name"
        {"name": "runtime_config", "tenant_scoped": True},   # durable operator runtime overrides; id = "tenant|key"
        {"name": "service_accounts", "tenant_scoped": True},  # machine identities (client_id + hashed secret + caps)
    ],
    "excluded": [
        {"name": "users_by_username",
         "reason": "derived index (tenant::normalized-username -> user id); keyed by the identifier, "
                   "value carries only the target id, so it cannot round-trip through the id-keyed "
                   "core. Rebuilt from `users` on restore — the data is fully recovered"},
        {"name": "persons_by_identifier",
         "reason": "derived index (identifier -> person id); same as users_by_username. Rebuilt from "
                   "`persons` on restore"},
        {"name": "refresh_tokens",
         "reason": "session/refresh tokens — keyed by jti with no id field; regenerated on the next "
                   "login, not durable data. Restoring stale sessions would at best log a user back "
                   "into a session they already rotated out of"},
        {"name": "login_attempts",
         "reason": "brute-force lockout counters — transient security state, keyed by tenant::user. "
                   "Restoring a stale count would wrongly lock or unlock an account; the live signal "
                   "rebuilds itself from real traffic in minutes"},
        {"name": "email_verifications",
         "reason": "single-use email-verification tokens — ephemeral, deleted on use, keyed by token "
                   "digest with no durable id. Re-sending mints a fresh one; nothing to restore"},
    ],
}
