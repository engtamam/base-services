"""What backup-service must capture to restore webhook-service, and what it may skip.

A pure declaration, read as a literal. The `safety:backup-coverage` gate checks it against the
collections the code actually writes, so a collection added later without a line here fails the build.

All the durable data is backed up: endpoints (the subscriptions and their signing secrets — lose
them and every integration silently stops receiving events), the outbound event log, the delivery
attempts, and the inbound dedup log. endpoints/events/deliveries are id-keyed; inbound is keyed by
"{source}::{provider_event_id}" (its dedup identity) and declares that key template so a restored
inbound event lands under the same key a repeat delivery would dedup against.
"""
from __future__ import annotations

from typing import Any

BACKUP: dict[str, Any] = {
    "object_prefix": "",
    "collections": [
        {"name": "endpoints", "tenant_scoped": True},     # subscriptions + signing secrets
        {"name": "events", "tenant_scoped": True},        # outbound event log
        {"name": "deliveries", "tenant_scoped": True},    # delivery attempts
        {"name": "inbound", "tenant_scoped": True, "key": "{source}::{provider_event_id}"},
    ],
    "excluded": [],
}
