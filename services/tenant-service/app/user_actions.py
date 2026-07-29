"""tenant-service — end-user (self-service) actions.

A customer manages their OWN tenant here: view it, edit its config, set its branding. These are the
`_SELF` routes (self_router, prefix /api/v1/tenant, gated by tenant.manage) — the tenant is taken
from the caller's token, never a path parameter, so a user can only ever touch their own. The
platform-operator routes (provision/suspend/delete any tenant, under platform.tenants) are NOT here —
those live in admin_actions. Every action re-checks tenant.manage AND re-scopes to the caller's tenant
at the route; this file only decides what the app shows.
"""
from __future__ import annotations

from typing import Any


USER_ACTIONS: list[dict[str, Any]] = [
    {"id": "tenant.view_self", "label": "View my organization", "method": "GET",
     "path": "/api/v1/tenant", "capability": "tenant.manage", "group": "Organization"},
    {"id": "tenant.update_config", "label": "Update my settings", "method": "PUT",
     "path": "/api/v1/tenant/config", "capability": "tenant.manage", "group": "Organization",
     "confirm": True},
    {"id": "tenant.update_branding", "label": "Update my branding", "method": "PUT",
     "path": "/api/v1/tenant/branding", "capability": "tenant.manage", "group": "Organization"},
    {"id": "tenant.close", "label": "Close my organization", "method": "POST",
     "path": "/api/v1/tenant/close", "capability": "tenant.manage", "group": "Organization",
     "confirm": True, "danger": True},
]
