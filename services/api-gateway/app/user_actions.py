"""api-gateway — end-user (self-service) actions: deliberately NONE (it is the app's HOST).

The gateway's only user-tier routes are the two catalog aggregators — GET /api/v1/admin/catalog and
GET /api/v1/app/catalog — which BUILD the panels by fanning out to every service's /internal/manifest
with the internal token held server-side. GET /api/v1/app/catalog IS the end-user app itself, not an
action inside it, so it is not declared here. Everything else the gateway exposes is public
(health/metrics), internal-token (routes/manifest, rate-limit unblock), or the catch-all proxy — none
of which is a user action. If the gateway ever grows a genuine per-user self route under a capability,
declare it here.
"""
from __future__ import annotations

from typing import Any


USER_ACTIONS: list[dict[str, Any]] = []
