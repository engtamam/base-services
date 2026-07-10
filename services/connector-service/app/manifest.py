# =============================================================================
# connector-service — Self-describing service manifest
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
        "capabilities": ["platform.connectors", "connectors.manage", "connectors.read"],
        "endpoints": [
            {"path": "/healthz", "tier": "public", "method": "GET"},
            {"path": "/readyz", "tier": "public", "method": "GET"},
            {"path": "/metrics", "tier": "public", "method": "GET"},
            {"path": "/internal/health", "tier": "internal", "method": "GET"},
            {"path": "/internal/manifest", "tier": "internal", "method": "GET"},
            {"path": "/internal/connectors/sync", "tier": "internal", "method": "POST"},
            {"path": "/api/v1/connectors/specs", "tier": "user", "method": "POST"},
            {"path": "/api/v1/connectors/specs", "tier": "user", "method": "GET"},
            {"path": "/api/v1/connectors/connect", "tier": "user", "method": "POST"},
            {"path": "/api/v1/connectors", "tier": "user", "method": "GET"},
            {"path": "/api/v1/connectors/{id}/health", "tier": "user", "method": "GET"},
            {"path": "/api/v1/connectors/{id}/disconnect", "tier": "user", "method": "POST"},
            {"path": "/api/v1/connectors/sync", "tier": "user", "method": "POST"},
            {"path": "/api/v1/connectors/runs", "tier": "user", "method": "GET"},
            {"path": "/api/v1/connectors/dead-letters", "tier": "user", "method": "GET"},
        ],
        "ports": ports or [],  # port -> driver, from the composition root
        "events": {"emitted": [], "consumed": []},
    }
