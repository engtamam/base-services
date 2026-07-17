"""feature-flag-service — end-user (self-service) actions: deliberately NONE.

Every user-tier route is operator/developer-scoped: `_ADMIN` (platform.flags / flags.manage) to
manage flags, segments, remote config, and maintenance, or `_READ` (…/flags.read) to inspect them.
The user-tier `/flags/evaluate` is a dry-run for a developer holding flags.read, not an end-user
action. A real end-user's flags are evaluated for them by the platform via the INTERNAL route
(/internal/flags/evaluate) — a service-to-service call, never a user surface. So there is no `_SELF`
route and no `flags.self` capability, and the end-user app shows nothing here.

The whole surface is the operator's (see admin_actions). This file makes the ABSENCE reviewed, not
forgotten.
"""
from __future__ import annotations

from typing import Any


USER_ACTIONS: list[dict[str, Any]] = []
