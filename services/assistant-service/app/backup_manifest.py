"""What backup-service must capture to restore assistant-service, and what it may skip.

A pure declaration, read as a literal. The `safety:backup-coverage` gate checks it against the
collections the code actually writes, so a collection added later without a line here fails the build.

Both collections are id-keyed (datastore key == row id). These are the customer's support
conversations and their messages — user content, restored under its own id. (The retrieval/answer
model is stateless; operator_secrets/runtime_config are the platform's re-enterable infra, not this
service's data, and are excluded from backup by design.)
"""
from __future__ import annotations

from typing import Any

BACKUP: dict[str, Any] = {
    "object_prefix": "",
    "collections": [
        {"name": "assistant_conversations", "tenant_scoped": True},
        {"name": "assistant_messages", "tenant_scoped": True},
        # Per-tenant AI connection definitions and their usage/cost ledger. Durable config + billing
        # data — both keyed by the bare connection id (id == key) and tenant-stamped. A restore must
        # keep them: losing ai_connections drops a tenant's configured LLM lines; losing
        # ai_connection_usage loses the metered cost record. (Rows written before the key migration
        # live under a legacy composite key — run POST /internal/assistant/connections/migrate-keys
        # once after upgrade to re-key them so a backup captures them.)
        {"name": "ai_connections", "tenant_scoped": True},
        {"name": "ai_connection_usage", "tenant_scoped": True},
    ],
    "excluded": [],
}
