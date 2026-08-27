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
     # A read, so the READ capability — not tenant.manage (write). The route accepts tenant.read OR
     # tenant.manage, so an owner still sees it; declaring the least-privilege cap is what lets a
     # tenant.read manager see the org in the app catalog at all.
     "path": "/api/v1/tenant", "capability": "tenant.read", "group": "Organization"},
    {"id": "tenant.update_config", "label": "Update my settings", "method": "PUT",
     "path": "/api/v1/tenant/config", "capability": "tenant.manage", "group": "Organization",
     "confirm": True},
    {"id": "tenant.update_branding", "label": "Update my branding", "method": "PUT",
     "path": "/api/v1/tenant/branding", "capability": "tenant.manage", "group": "Organization"},
    {"id": "tenant.close", "label": "Close my organization", "method": "POST",
     "path": "/api/v1/tenant/close", "capability": "tenant.manage", "group": "Organization",
     "confirm": True, "danger": True},
]
