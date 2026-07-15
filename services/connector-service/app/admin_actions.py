"""connector-service — admin actions (the complete operator surface).

Every user-tier route is operator-scoped (platform.connectors / connectors.manage for mutations,
connectors.read for the views); there is no `_SELF` route. The scheduled sync fan-out is
internal-token (/internal/connectors/sync), not a panel action. What the operator drives: author
connector specs, connect a third party, list and health-check connections, disconnect one, trigger
an on-demand sync, and inspect runs and the dead-letter queue. Disconnect is danger + confirm — it
tears down live credentials. Every action re-checks its capability at the route; this file only
decides what is shown.
"""
from __future__ import annotations

from typing import Any


ADMIN_ACTIONS: list[dict[str, Any]] = [
    # ── Specs ────────────────────────────────────────────────────────────────
    {"id": "connectors.list_specs", "label": "List connector specs", "method": "GET",
     "path": "/api/v1/connectors/specs", "capability": "platform.connectors", "group": "Specs"},
    {"id": "connectors.create_spec", "label": "Create a connector spec", "method": "POST",
     "path": "/api/v1/connectors/specs", "capability": "platform.connectors", "group": "Specs",
     "confirm": True},
    # ── Connections ──────────────────────────────────────────────────────────
    {"id": "connectors.connect", "label": "Connect a third party", "method": "POST",
     "path": "/api/v1/connectors/connect", "capability": "platform.connectors", "group": "Connections",
     "confirm": True},
    {"id": "connectors.list", "label": "List connections", "method": "GET",
     "path": "/api/v1/connectors", "capability": "platform.connectors", "group": "Connections"},
    {"id": "connectors.health", "label": "View connection health", "method": "GET",
     "path": "/api/v1/connectors/{id}/health", "capability": "platform.connectors",
     "group": "Connections",
     "params": [{"name": "id", "in": "path", "type": "string", "required": True}]},
    {"id": "connectors.disconnect", "label": "Disconnect", "method": "POST",
     "path": "/api/v1/connectors/{id}/disconnect", "capability": "platform.connectors",
     "group": "Connections", "danger": True, "confirm": True,
     "params": [{"name": "id", "in": "path", "type": "string", "required": True,
                 "help": "tears down the stored credentials — a reconnect requires re-authorizing"}]},
    # ── Sync ─────────────────────────────────────────────────────────────────
    {"id": "connectors.sync", "label": "Trigger an on-demand sync", "method": "POST",
     "path": "/api/v1/connectors/sync", "capability": "platform.connectors", "group": "Sync",
     "confirm": True},
    {"id": "connectors.runs", "label": "List sync runs", "method": "GET",
     "path": "/api/v1/connectors/runs", "capability": "platform.connectors", "group": "Sync"},
    {"id": "connectors.dead_letters", "label": "List dead-letters", "method": "GET",
     "path": "/api/v1/connectors/dead-letters", "capability": "platform.connectors", "group": "Sync"},
]
