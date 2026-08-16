"""What backup-service must capture to restore notification-service, and what it may skip.

A pure declaration, read as a literal. The `safety:backup-coverage` gate checks it against the
collections the code actually writes, so a collection added later without a line here fails the build.

The durable data is the message templates (email/SMS and WhatsApp), the recipients' delivery
PREFERENCES (an unsubscribe is a consent-adjacent record that must survive), the in-app inbox, and
the delivery history. Three collections are keyed by something other than a row id and declare a key
template: templates by "{key}::{locale}", whatsapp_templates by "{tenant}|{name}|{language}", and
preferences by "{user}". inapp and deliveries are id-keyed. The per-send idempotency table is
excluded — it regenerates and holds no data. None of these rows carry a tenant field (they are keyed
by user/recipient/template identity), so all are tenant_scoped=False except whatsapp_templates.
"""
from __future__ import annotations

from typing import Any

BACKUP: dict[str, Any] = {
    "object_prefix": "",
    "collections": [
        {"name": "templates", "tenant_scoped": False, "key": "{key}::{locale}"},
        {"name": "whatsapp_templates", "tenant_scoped": True, "key": "{tenant}|{name}|{language}"},
        {"name": "preferences", "tenant_scoped": False, "key": "{user}"},
        {"name": "inapp", "tenant_scoped": False},
        {"name": "deliveries", "tenant_scoped": False},
        # The append-only delivery/read receipt log. Backed up because it is the DURABLE catch-up
        # source: the status bus is at-most-once, so this ordered log is how a consumer reconciles
        # receipts it missed — losing it on a restore silently loses that history. Id-keyed.
        {"name": "notify_receipts", "tenant_scoped": False},
    ],
    "excluded": [
        {"name": "notify_idempo",
         "reason": "per-send idempotency keys (idempotency_key -> {result, ts}); regenerated on the "
                   "next send, not data. Restoring stale ones would at worst suppress a resend"},
    ],
}
