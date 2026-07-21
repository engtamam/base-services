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
    # ── Operator controls: credentials and policy, changeable after launch ────
    # A panel that can only ACT is half a panel: the reads are declared too, so an operator can
    # see which credential is loaded before replacing it. No action here returns a value — the
    # list shows a four-character hint, and safety:no-secret-reads fails the build if that
    # ever changes.
    {
        "id": "operator.list_secrets", "label": "Connector credentials", "method": "GET",
        "path": "/api/v1/operator/secrets", "capability": "platform.secrets",
        "group": "Credentials",
    },
    {
        "id": "operator.set_secret", "label": "Set / rotate a connector credential", "method": "PUT",
        "path": "/api/v1/operator/secrets", "capability": "platform.secrets",
        "group": "Credentials", "confirm": True,
        "params": [
            {"name": "name", "in": "body", "type": "string", "required": True,
             "help": "the setting it replaces, e.g. SPEC_CLIENT_SECRET:spec_abc123, the OAuth client secret for one spec"},
            {"name": "value", "in": "body", "type": "string", "required": True,
             "help": "write-only: it is sealed on arrival and no route can read it back"},
            {"name": "tenant", "in": "body", "type": "string", "required": False,
             "help": "leave empty for platform-wide"},
        ],
    },
    {
        "id": "operator.clear_secret", "label": "Remove a connector credential", "method": "DELETE",
        "path": "/api/v1/operator/secrets/{name}", "capability": "platform.secrets",
        "group": "Credentials", "confirm": True,
        "params": [{"name": "name", "in": "path", "type": "string", "required": True}],
    },
    {
        "id": "operator.get_config", "label": "Runtime settings", "method": "GET",
        "path": "/api/v1/operator/config", "capability": "platform.config", "group": "Settings",
    },
    {
        "id": "operator.set_config", "label": "Change a runtime setting", "method": "PUT",
        "path": "/api/v1/operator/config", "capability": "platform.config",
        "group": "Settings", "confirm": True,
        "params": [
            {"name": "key", "in": "body", "type": "string", "required": True,
             "help": "e.g. SYNC_MAX_ATTEMPTS, or CIRCUIT_FAILURE_THRESHOLD"},
            {"name": "value", "in": "body", "type": "string", "required": True},
            {"name": "tenant", "in": "body", "type": "string", "required": False},
        ],
    },
    {
        "id": "operator.clear_config", "label": "Reset a setting to its default", "method": "DELETE",
        "path": "/api/v1/operator/config/{key}", "capability": "platform.config",
        "group": "Settings", "confirm": True,
        "params": [{"name": "key", "in": "path", "type": "string", "required": True}],
    },
]
