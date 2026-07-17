"""audit-service — end-user (self-service) actions: a user's OWN audit trail.

The require_user routes query and export the caller's OWN tenant trail (the tenant is taken from the
token and pushed into the filter, so a user only ever sees their own events). `audit.view` is the
advertised capability for reading the trail. The operator surface — verify the hash chain and the
retention purge — enforces platform.audit and lives in admin_actions (append is internal-token only).
The app is a map — each route re-checks and re-scopes to the caller; this file only decides what shows.
"""
from __future__ import annotations

from typing import Any


USER_ACTIONS: list[dict[str, Any]] = [
    {"id": "audit.my_trail", "label": "My activity log", "method": "GET",
     "path": "/api/v1/audit", "capability": "audit.view", "group": "Audit"},
    {"id": "audit.export", "label": "Export my activity log", "method": "GET",
     "path": "/api/v1/audit/export", "capability": "audit.view", "group": "Audit"},
]
