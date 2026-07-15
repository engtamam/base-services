"""feature-flag-service — admin actions (the complete operator surface).

Every user-tier route here is operator-scoped: platform.flags / flags.manage for mutations (_ADMIN),
plus flags.read for the views (_READ). There is no `_SELF` route — a person never manages their own
flags; a subject only ever receives an EVALUATION via the internal route. So the whole surface is the
panel's: manage flags, segments, remote config (with history + rollback), targeted overrides, and the
global maintenance switch. The dangerous ones — archive a flag, delete a segment, turn on maintenance
— are danger + confirm. Evaluation is internal-token; the user-tier evaluate is a dry-run test.
Every action re-checks its capability at the route; this file only decides what is shown.
"""
from __future__ import annotations

from typing import Any


ADMIN_ACTIONS: list[dict[str, Any]] = [
    # ── Flags ────────────────────────────────────────────────────────────────
    {"id": "flags.list", "label": "List flags", "method": "GET",
     "path": "/api/v1/flags", "capability": "platform.flags", "group": "Flags",
     "params": [{"name": "include_archived", "in": "query", "type": "boolean", "required": False}]},
    {"id": "flags.stale", "label": "List stale flags", "method": "GET",
     "path": "/api/v1/flags/stale", "capability": "platform.flags", "group": "Flags",
     "params": [{"name": "days", "in": "query", "type": "number", "required": False}]},
    {"id": "flags.upsert", "label": "Create / update a flag", "method": "POST",
     "path": "/api/v1/flags", "capability": "platform.flags", "group": "Flags", "confirm": True,
     "params": [{"name": "key", "in": "body", "type": "string", "required": True}]},
    {"id": "flags.archive", "label": "Archive a flag", "method": "DELETE",
     "path": "/api/v1/flags/{key}", "capability": "platform.flags", "group": "Flags",
     "danger": True, "confirm": True,
     "params": [{"name": "key", "in": "path", "type": "string", "required": True}]},
    {"id": "flags.evaluate", "label": "Evaluate a flag (dry run)", "method": "POST",
     "path": "/api/v1/flags/evaluate", "capability": "platform.flags", "group": "Flags"},
    # ── Overrides ────────────────────────────────────────────────────────────
    {"id": "flags.set_override", "label": "Set a targeted override", "method": "POST",
     "path": "/api/v1/flags/{key}/overrides", "capability": "platform.flags", "group": "Overrides",
     "confirm": True,
     "params": [{"name": "key", "in": "path", "type": "string", "required": True}]},
    {"id": "flags.clear_override", "label": "Clear a targeted override", "method": "DELETE",
     "path": "/api/v1/flags/{key}/overrides", "capability": "platform.flags", "group": "Overrides",
     "confirm": True,
     "params": [{"name": "key", "in": "path", "type": "string", "required": True},
                {"name": "scope", "in": "query", "type": "string", "required": True},
                {"name": "subject", "in": "query", "type": "string", "required": True}]},
    # ── Segments ─────────────────────────────────────────────────────────────
    {"id": "flags.list_segments", "label": "List segments", "method": "GET",
     "path": "/api/v1/segments", "capability": "platform.flags", "group": "Segments"},
    {"id": "flags.upsert_segment", "label": "Create / update a segment", "method": "POST",
     "path": "/api/v1/segments", "capability": "platform.flags", "group": "Segments", "confirm": True},
    {"id": "flags.delete_segment", "label": "Delete a segment", "method": "DELETE",
     "path": "/api/v1/segments/{key}", "capability": "platform.flags", "group": "Segments",
     "danger": True, "confirm": True,
     "params": [{"name": "key", "in": "path", "type": "string", "required": True}]},
    # ── Remote config ────────────────────────────────────────────────────────
    {"id": "flags.get_config", "label": "View config value", "method": "GET",
     "path": "/api/v1/config/{key}", "capability": "platform.flags", "group": "Remote config",
     "params": [{"name": "key", "in": "path", "type": "string", "required": True}]},
    {"id": "flags.config_history", "label": "View config history", "method": "GET",
     "path": "/api/v1/config/{key}/history", "capability": "platform.flags", "group": "Remote config",
     "params": [{"name": "key", "in": "path", "type": "string", "required": True}]},
    {"id": "flags.put_config", "label": "Set a config value", "method": "PUT",
     "path": "/api/v1/config/{key}", "capability": "platform.flags", "group": "Remote config",
     "confirm": True,
     "params": [{"name": "key", "in": "path", "type": "string", "required": True}]},
    {"id": "flags.rollback_config", "label": "Roll back config to a prior version", "method": "POST",
     "path": "/api/v1/config/{key}/rollback", "capability": "platform.flags", "group": "Remote config",
     "danger": True, "confirm": True,
     "params": [{"name": "key", "in": "path", "type": "string", "required": True}]},
    # ── Maintenance ──────────────────────────────────────────────────────────
    {"id": "flags.get_maintenance", "label": "View maintenance state", "method": "GET",
     "path": "/api/v1/maintenance", "capability": "platform.flags", "group": "Maintenance"},
    {"id": "flags.set_maintenance", "label": "Set maintenance mode", "method": "PUT",
     "path": "/api/v1/maintenance", "capability": "platform.flags", "group": "Maintenance",
     "danger": True, "confirm": True},
]
