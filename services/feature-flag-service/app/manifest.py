# =============================================================================
# feature-flag-service — Self-describing service manifest
# -----------------------------------------------------------------------------
# A machine-readable descriptor so a gateway/platform can discover what this
# service is, what it exposes, which ports (adapters) it uses, and which events
# it emits/consumes. Served at GET /internal/manifest. Phase 1: ports/events are
# empty; later phases populate them.
# =============================================================================
from __future__ import annotations

from typing import Any

from app.config import Settings


def build_manifest(
    settings: Settings, ports: list[dict[str, str]] | None = None
) -> dict[str, Any]:
    return {
        "name": settings.SERVICE_NAME,
        "version": settings.SERVICE_VERSION,
        "env": settings.ENV,
        "capabilities": ["platform.flags", "flags.manage", "flags.read"],
        "endpoints": [
            {"path": "/healthz", "tier": "public", "method": "GET"},
            {"path": "/readyz", "tier": "public", "method": "GET"},
            {"path": "/metrics", "tier": "public", "method": "GET"},
            {"path": "/internal/health", "tier": "internal", "method": "GET"},
            {"path": "/internal/manifest", "tier": "internal", "method": "GET"},
            {"path": "/internal/flags/evaluate", "tier": "internal", "method": "POST"},
            {"path": "/internal/flags/{key}", "tier": "internal", "method": "GET"},
            {"path": "/internal/config/{key}", "tier": "internal", "method": "GET"},
            {"path": "/internal/maintenance", "tier": "internal", "method": "GET"},
            {"path": "/api/v1/flags", "tier": "user", "method": "POST"},
            {"path": "/api/v1/flags", "tier": "user", "method": "GET"},
            {"path": "/api/v1/flags/stale", "tier": "user", "method": "GET"},
            {"path": "/api/v1/flags/{key}", "tier": "user", "method": "DELETE"},
            {"path": "/api/v1/flags/{key}/overrides", "tier": "user", "method": "POST"},
            {"path": "/api/v1/flags/evaluate", "tier": "user", "method": "POST"},
            {"path": "/api/v1/segments", "tier": "user", "method": "POST"},
            {"path": "/api/v1/segments", "tier": "user", "method": "GET"},
            {"path": "/api/v1/segments/{key}", "tier": "user", "method": "DELETE"},
            {"path": "/api/v1/config/{key}", "tier": "user", "method": "PUT"},
            {"path": "/api/v1/config/{key}", "tier": "user", "method": "GET"},
            {"path": "/api/v1/config/{key}/history", "tier": "user", "method": "GET"},
            {"path": "/api/v1/config/{key}/rollback", "tier": "user", "method": "POST"},
            {"path": "/api/v1/maintenance", "tier": "user", "method": "PUT"},
            {"path": "/api/v1/maintenance", "tier": "user", "method": "GET"},
        ],
        "ports": ports or [],  # port -> driver, from the composition root
        "events": {"emitted": [], "consumed": []},
    }
