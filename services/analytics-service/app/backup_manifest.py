"""What backup-service must capture to restore analytics-service, and what it may skip.

A pure declaration, read as a literal. The `safety:backup-coverage` gate checks it against the
collections the code actually writes, so a collection added later without a line here fails the build.

The durable data is the raw events (the source of truth this service aggregates), the saved report
definitions (operator config), and the pre-aggregated rollups. events and reports are id-keyed;
rollups are keyed by their own composite `key` field, declared as a template. The rollups are
strictly a derivative of the events, but they are cheap to carry and expensive to recompute, so we
restore them directly rather than re-aggregating.

Two collections are excluded. event_seen is per-event ingest idempotency (event_id -> {id}); it
self-heals from the at-least-once ingest stream. dim_cardinality is a derived cardinality index whose
row is a bare {values:[...]} carrying none of its own key — not round-trippable, and rebuilt as
events flow.
"""
from __future__ import annotations

from typing import Any

BACKUP: dict[str, Any] = {
    "object_prefix": "",
    "collections": [
        {"name": "events", "tenant_scoped": True},
        {"name": "rollups", "tenant_scoped": True, "key": "{key}"},
        {"name": "reports", "tenant_scoped": True},
    ],
    "excluded": [
        {"name": "event_seen",
         "reason": "per-event ingest idempotency (event_id -> {id}); self-heals from the "
                   "at-least-once ingest stream, holds no data of its own"},
        {"name": "dim_cardinality",
         "reason": "derived cardinality index (tenant|metric|dim -> {values}); the row carries none "
                   "of its own key, so it cannot round-trip. Rebuilt from events as they flow"},
    ],
}
