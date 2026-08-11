# =============================================================================
# compliance-service — Self-describing service manifest
# -----------------------------------------------------------------------------
# A machine-readable descriptor so a gateway/platform can discover what this
# service is, what it exposes, which ports (adapters) it uses, and which events
# it emits/consumes. Served at GET /internal/manifest. Phase 1: ports/events are
# empty; later phases populate them.
# =============================================================================
from __future__ import annotations

from typing import Any

from app.config import Settings


def _admin_actions() -> list[dict[str, Any]]:
    """This service's declared admin actions (docs/contracts/admin-actions.schema.json), or [] if it
    declares none. Opt-in: a service with no app/admin_actions.py simply exposes nothing to the
    panel. The panel is a map — these decide what is SHOWN, never what an operator may DO."""
    try:
        from app.admin_actions import ADMIN_ACTIONS
    except Exception:
        return []
    return list(ADMIN_ACTIONS)


def _user_actions() -> list[dict[str, Any]]:
    """This service's declared end-user (self-service) actions
    (docs/contracts/user-actions.schema.json), or [] if it declares none. Opt-in: aggregated into
    GET /api/v1/app/catalog. The app is a map — these decide what is SHOWN, never what a user may DO."""
    try:
        from app.user_actions import USER_ACTIONS
    except Exception:
        return []
    return list(USER_ACTIONS)


def _backup() -> dict[str, Any]:
    """This service's backup declaration (app/backup_manifest.py), or an empty one.

    Published in the manifest so the backup fan-out discovers what to capture the same way the DSAR
    fan-out discovers who holds a person — a holder nobody declares is one nobody backs up."""
    try:
        from app.backup_manifest import BACKUP
    except Exception:
        return {"object_prefix": "", "collections": [], "excluded": []}
    return BACKUP


def build_manifest(
    settings: Settings, ports: list[dict[str, str]] | None = None
) -> dict[str, Any]:
    return {
        "name": settings.SERVICE_NAME,
        "version": settings.SERVICE_VERSION,
        "env": settings.ENV,
        "capabilities": ["platform.compliance", "compliance.manage", "compliance.read",
                         "compliance.self", "platform.secrets", "platform.config"],
        "endpoints": [
            {"path": "/healthz", "tier": "public", "method": "GET"},
            {"path": "/readyz", "tier": "public", "method": "GET"},
            {"path": "/metrics", "tier": "public", "method": "GET"},
            {"path": "/internal/health", "tier": "internal", "method": "GET"},
            {"path": "/internal/manifest", "tier": "internal", "method": "GET"},
            {"path": "/internal/privacy/erase", "tier": "internal", "method": "POST",
             # DECLARED, because a data map built from manifests is how a DSAR fan-out
             # finds who holds a person. An undeclared holder is never asked, never acks,
             # and is therefore never MISSED — the request closes green on an erasure
             # that did not happen.
             "erasure_category": "subject"},
            # Backup fan-out (backup-service orchestrates): export + verify + import the legal
            # records (consent, DSAR, RoPA, breaches, data map). Declared so the fan-out discovers
            # this holder — an undeclared holder is one nobody backs up.
            {"path": "/internal/backup/export", "tier": "internal", "method": "POST"},
            {"path": "/internal/backup/import", "tier": "internal", "method": "POST"},
            {"path": "/internal/backup/verify", "tier": "internal", "method": "POST"},
            {"path": "/internal/compliance/consent-check", "tier": "internal", "method": "GET",
             # Another SERVICE must call this. safety:contract-callers fails if none declares it.
             "caller": "service"},
            {"path": "/internal/compliance/hold-check", "tier": "internal", "method": "GET",
             "caller": "service"},
            {"path": "/internal/compliance/held-refs", "tier": "internal", "method": "GET",
             "caller": "service"},
            {"path": "/internal/compliance/dsar/{id}/target-ack", "tier": "internal", "method": "POST"},
            {"path": "/internal/compliance/consent/sweep-lapses", "tier": "internal", "method": "POST"},
            {"path": "/internal/compliance/consent/migrate-purpose-keys", "tier": "internal", "method": "POST"},
            {"path": "/api/v1/compliance/consent/grant", "tier": "user", "method": "POST"},
            {"path": "/api/v1/compliance/consent/withdraw", "tier": "user", "method": "POST"},
            {"path": "/api/v1/compliance/consent/history", "tier": "user", "method": "GET"},
            {"path": "/api/v1/compliance/me/dsar", "tier": "user", "method": "POST"},
            {"path": "/api/v1/compliance/dsar", "tier": "user", "method": "POST"},
            {"path": "/api/v1/compliance/dsar", "tier": "user", "method": "GET"},
            {"path": "/api/v1/compliance/dsar/{id}/verify", "tier": "user", "method": "POST"},
            {"path": "/api/v1/compliance/dsar/{id}/process", "tier": "user", "method": "POST"},
            {"path": "/api/v1/compliance/legal-holds", "tier": "user", "method": "POST"},
            {"path": "/api/v1/compliance/legal-holds", "tier": "user", "method": "GET"},
            {"path": "/api/v1/compliance/legal-holds/{id}/release", "tier": "user", "method": "POST"},
            {"path": "/api/v1/compliance/data-targets", "tier": "user", "method": "POST"},
            {"path": "/api/v1/compliance/data-targets", "tier": "user", "method": "GET"},
            {"path": "/api/v1/compliance/data-targets/{id}", "tier": "user", "method": "DELETE"},
            {"path": "/api/v1/compliance/data-targets/unreachable", "tier": "user", "method": "GET"},
            {"path": "/api/v1/compliance/ropa", "tier": "user", "method": "POST"},
            {"path": "/api/v1/compliance/ropa", "tier": "user", "method": "GET"},
            {"path": "/api/v1/compliance/ropa/{id}", "tier": "user", "method": "DELETE"},
            {"path": "/api/v1/compliance/breaches", "tier": "user", "method": "POST"},
            {"path": "/api/v1/compliance/breaches", "tier": "user", "method": "GET"},
            {"path": "/api/v1/compliance/breaches/{id}/notify", "tier": "user", "method": "POST"},
            # Legal / content pages: author (draft/publish) + the structured viewer + version history.
            {"path": "/api/v1/compliance/legal/{doc_id}/versions/{version}", "tier": "user", "method": "PUT"},
            {"path": "/api/v1/compliance/legal/{doc_id}/versions/{version}/publish", "tier": "user", "method": "POST"},
            {"path": "/api/v1/compliance/legal", "tier": "user", "method": "GET"},
            {"path": "/api/v1/compliance/legal/{doc_id}", "tier": "user", "method": "GET"},
            {"path": "/api/v1/compliance/legal/{doc_id}/history", "tier": "user", "method": "GET"},
            {"path": "/api/v1/compliance/me/legal/outstanding", "tier": "user", "method": "GET"},
            {"path": "/api/v1/compliance/me/legal/{doc_id}/accept", "tier": "user", "method": "POST"},
            # PDPL template pack: list + fill-in-the-blank instantiate into a legal page.
            {"path": "/api/v1/compliance/legal/templates", "tier": "user", "method": "GET"},
            {"path": "/api/v1/compliance/legal/templates/{template_id}/instantiate", "tier": "user", "method": "POST"},
            # Product provisioning wires the data map + fetches a DSAR verify code, at the internal tier.
            {"path": "/internal/compliance/data-targets", "tier": "internal", "method": "POST"},
            {"path": "/internal/compliance/dsar/{id}/verification-code", "tier": "internal", "method": "GET"},
            {"path": "/internal/compliance/dsar/run", "tier": "internal", "method": "POST"},
            {"path": "/internal/compliance/retention/run", "tier": "internal", "method": "POST"},
            # The operator surface: credentials and policy an operator changes after launch.
            {"path": "/api/v1/operator/secrets", "tier": "user", "method": "GET"},
            {"path": "/api/v1/operator/secrets", "tier": "user", "method": "PUT"},
            {"path": "/api/v1/operator/secrets/{}", "tier": "user", "method": "DELETE"},
            {"path": "/api/v1/operator/config", "tier": "user", "method": "GET"},
            {"path": "/api/v1/operator/config", "tier": "user", "method": "PUT"},
            {"path": "/api/v1/operator/config/{}", "tier": "user", "method": "DELETE"},
        ],
        "ports": ports or [],  # port -> driver, from the composition root
        "events": {"emitted": [], "consumed": []},
        "admin_actions": _admin_actions(),
        "user_actions": _user_actions(),
        "backup": _backup(),   # what backup-service must capture to restore this service
    }
