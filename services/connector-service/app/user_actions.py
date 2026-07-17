"""connector-service — end-user (self-service) actions: a customer's OWN integrations.

Connecting a third party (Slack, HubSpot, any OAuth2/REST provider) is a customer/developer concern,
tenant-scoped from the token. The routes accept the per-tenant caps connectors.manage (writes) /
connectors.read (reads), so the same routes serve a platform operator (platform.connectors,
admin_actions) and a customer (here): browse available connector specs, connect and disconnect,
list connections and their health, trigger a sync, and review sync runs + the dead-letter queue.
Authoring a connector SPEC (defining a new connector type) is a developer/operator surface and stays
in admin_actions. Each route re-checks its capability and re-scopes to the caller's tenant.
"""
from __future__ import annotations

from typing import Any


USER_ACTIONS: list[dict[str, Any]] = [
    {"id": "connectors.list_specs", "label": "Available connectors", "method": "GET",
     "path": "/api/v1/connectors/specs", "capability": "connectors.read", "group": "Connectors"},
    {"id": "connectors.connect", "label": "Connect an integration", "method": "POST",
     "path": "/api/v1/connectors/connect", "capability": "connectors.manage", "group": "Connectors",
     "confirm": True},
    {"id": "connectors.list", "label": "My connections", "method": "GET",
     "path": "/api/v1/connectors", "capability": "connectors.read", "group": "Connectors"},
    {"id": "connectors.health", "label": "Connection health", "method": "GET",
     "path": "/api/v1/connectors/{id}/health", "capability": "connectors.read", "group": "Connectors",
     "params": [{"name": "id", "in": "path", "type": "string", "required": True}]},
    {"id": "connectors.disconnect", "label": "Disconnect", "method": "POST",
     "path": "/api/v1/connectors/{id}/disconnect", "capability": "connectors.manage",
     "group": "Connectors", "danger": True, "confirm": True,
     "params": [{"name": "id", "in": "path", "type": "string", "required": True}]},
    {"id": "connectors.sync", "label": "Sync now", "method": "POST",
     "path": "/api/v1/connectors/sync", "capability": "connectors.manage", "group": "Sync",
     "confirm": True},
    {"id": "connectors.runs", "label": "Sync history", "method": "GET",
     "path": "/api/v1/connectors/runs", "capability": "connectors.read", "group": "Sync"},
    {"id": "connectors.dead_letters", "label": "Failed records", "method": "GET",
     "path": "/api/v1/connectors/dead-letters", "capability": "connectors.read", "group": "Sync"},
]
