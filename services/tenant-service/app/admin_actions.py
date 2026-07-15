"""tenant-service — the admin actions this service declares to the platform panel.

Each service owns THIS file: it is the one place its operator controls are listed, right next to the
code that serves them. The manifest builder includes it; the platform aggregator collects it across
every service. There is no central list to keep in sync — a new admin route adds one entry here.

INVARIANT: this file decides what the panel SHOWS, never what an operator may DO. Every `path` below
is an existing route that re-checks its `capability` server-side. See phase-33.
"""
from __future__ import annotations

from typing import Any

# `admin_actions` follows docs/contracts/admin-actions.schema.json. Nothing hardcoded that belongs
# in config — these are declarations OF the routes, which are themselves the code.
ADMIN_ACTIONS: list[dict[str, Any]] = [
    {
        "id": "tenant.provision",
        "label": "Provision tenant",
        "method": "POST",
        "path": "/api/v1/tenants",
        "capability": "platform.tenants",
        "group": "Tenancy",
        "params": [
            {"name": "slug", "in": "body", "type": "string", "required": True},
            {"name": "name", "in": "body", "type": "string", "required": True},
        ],
    },
    {
        "id": "tenant.update_config",
        "label": "Update tenant config",
        "method": "PUT",
        "path": "/api/v1/tenants/{tenant_id}/config",
        "capability": "platform.tenants",
        "group": "Tenancy",
        "params": [
            {"name": "tenant_id", "in": "path", "type": "string", "required": True},
            {"name": "config", "in": "body", "type": "object", "required": True},
        ],
    },
    {
        "id": "tenant.update_branding",
        "label": "Update branding",
        "method": "PUT",
        "path": "/api/v1/tenants/{tenant_id}/branding",
        "capability": "platform.tenants",
        "group": "Tenancy",
        "params": [
            {"name": "tenant_id", "in": "path", "type": "string", "required": True},
            {"name": "branding", "in": "body", "type": "object", "required": True},
        ],
    },
    {
        "id": "tenant.suspend",
        "label": "Suspend tenant",
        "method": "POST",
        "path": "/api/v1/tenants/{tenant_id}/suspend",
        "capability": "platform.tenants",
        "group": "Lifecycle",
        "confirm": True,
        "danger": True,
        "params": [{"name": "tenant_id", "in": "path", "type": "string", "required": True}],
    },
    {
        "id": "tenant.reactivate",
        "label": "Reactivate tenant",
        "method": "POST",
        "path": "/api/v1/tenants/{tenant_id}/reactivate",
        "capability": "platform.tenants",
        "group": "Lifecycle",
        "confirm": True,
        "params": [{"name": "tenant_id", "in": "path", "type": "string", "required": True}],
    },
    {
        "id": "tenant.delete",
        "label": "Delete tenant (tombstone)",
        "method": "DELETE",
        "path": "/api/v1/tenants/{tenant_id}",
        "capability": "platform.tenants",
        "group": "Lifecycle",
        "confirm": True,
        "danger": True,
        "params": [{"name": "tenant_id", "in": "path", "type": "string", "required": True}],
    },
    {
        "id": "tenant.export",
        "label": "Export / offboard tenant",
        "method": "GET",
        "path": "/api/v1/tenants/{tenant_id}/export",
        "capability": "platform.tenants",
        "group": "Lifecycle",
        "params": [{"name": "tenant_id", "in": "path", "type": "string", "required": True}],
    },
]
