"""api-gateway — admin actions: deliberately NONE (it is the panel's HOST, not a target).

The gateway's only user-tier route is GET /api/v1/admin/catalog — the aggregator that BUILDS the
panel by fanning out to every service's /internal/manifest with the internal token held server-side.
That is the panel itself, not an action inside it, so it is not declared here. Everything else the
gateway exposes is public (health/metrics), internal-token (routes/manifest, rate-limit unblock), or
the catch-all proxy — none of which is an operator action. If the gateway ever grows a genuine
self-management route under a platform capability, declare it here.
"""
from __future__ import annotations

from typing import Any


ADMIN_ACTIONS: list[dict[str, Any]] = []
