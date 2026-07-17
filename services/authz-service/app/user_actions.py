"""authz-service — end-user (self-service) actions: deliberately NONE.

authz is THE control plane, operated entirely by a platform operator: every route is `_ADMIN`
(platform.authz / authz.manage) — create/edit/delete roles and policies, and assign roles to a
subject. Subject-role lookups (`/subjects/{subject}/roles`) are operator reads by parameter, not a
"my own roles" self route, and effective-permission resolution is an internal-token PDP call
(/internal/authz/*), not a user surface. So there is no `_SELF` / `require_user` route and no
`authz.self` capability, and the end-user app shows nothing here.

The whole surface is the operator's (see admin_actions). This file makes the ABSENCE a reviewed
decision: if a genuine self route is ever added (e.g. `GET /api/v1/me/permissions` under an
`authz.self` capability), declare it here.
"""
from __future__ import annotations

from typing import Any


USER_ACTIONS: list[dict[str, Any]] = []
