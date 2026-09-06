# =============================================================================
# assistant-service — Self-describing service manifest
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


def _backup() -> dict:
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
        # FILL THIS. An empty list is a CLAIM — "this service grants no capabilities" — that an
        # adopter's client app reads as fact and renders nothing from. A blank app is the usual
        # symptom of a service that shipped this untouched. If a service genuinely grants none,
        # leaving it empty is correct; the point is that it must be a decision, not a leftover.
        # platform.secrets is deliberately separate from platform.config: setting the model
        # provider's key and changing a retention period are not the same privilege, and a role
        # that conflates them hands every settings-editor the ability to point the assistant at an
        # endpoint of their choosing.
        "capabilities": ["assistant.ask", "platform.assistant",
                         "platform.secrets", "platform.config"],
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
            {"path": "/api/v1/admin/ping", "tier": "user", "method": "GET"},
            {"path": "/api/v1/whoami", "tier": "user", "method": "GET"},
            {"path": "/api/v1/assistant/ask", "tier": "user", "method": "POST"},
            {"path": "/api/v1/assistant/help", "tier": "user", "method": "GET"},
            {"path": "/api/v1/assistant/conversations", "tier": "user", "method": "GET"},
            {"path": "/api/v1/assistant/conversations/{id}", "tier": "user", "method": "GET"},
            {"path": "/api/v1/assistant/conversations/{id}", "tier": "user", "method": "DELETE"},
            {"path": "/api/v1/assistant/branding", "tier": "user", "method": "GET"},
            {"path": "/api/v1/assistant/admin/kb/status", "tier": "admin", "method": "GET"},
            {"path": "/api/v1/assistant/admin/kb/reload", "tier": "admin", "method": "POST"},
            # The operator surface: credentials and policy an operator changes after launch.
            # A route table built from manifests that omits where credentials are set hides the
            # highest-privilege surface this service has.
            {"path": "/api/v1/assistant/operator/secrets", "tier": "admin", "method": "GET"},
            {"path": "/api/v1/assistant/operator/secrets", "tier": "admin", "method": "PUT"},
            {"path": "/api/v1/assistant/operator/secrets/{name}", "tier": "admin", "method": "DELETE"},
            {"path": "/api/v1/assistant/operator/config", "tier": "admin", "method": "GET"},
            {"path": "/api/v1/assistant/operator/config", "tier": "admin", "method": "PUT"},
            {"path": "/api/v1/assistant/operator/config/{key}", "tier": "admin", "method": "DELETE"},
            {"path": "/internal/assistant/retention", "tier": "internal", "method": "POST"},
            # Customer-service AI (advisory, opt-in via a real MODEL_DRIVER): triage a new ticket and
            # draft a reply. ticket-service's SUGGEST_DRIVER=http points here.
            {"path": "/internal/assistant/triage", "tier": "internal", "method": "POST"},
            {"path": "/internal/assistant/draft", "tier": "internal", "method": "POST"},
            # AI connections: N isolated, per-tenant LLM lines, each with its own provider/prompt/price.
            {"path": "/internal/assistant/connections", "tier": "internal", "method": "POST"},
            {"path": "/internal/assistant/connections", "tier": "internal", "method": "GET"},
            {"path": "/internal/assistant/connections/{cid}", "tier": "internal", "method": "GET"},
            {"path": "/internal/assistant/connections/{cid}", "tier": "internal", "method": "DELETE"},
            {"path": "/internal/assistant/connections/{cid}/invoke", "tier": "internal", "method": "POST"},
            {"path": "/internal/assistant/connections/{cid}/usage", "tier": "internal", "method": "GET"},
            # Backup fan-out (backup-service orchestrates): export + verify + import this
            # service's data. Declared so the fan-out discovers this holder — an undeclared holder
            # is one nobody backs up.
            {"path": "/internal/backup/export", "tier": "internal", "method": "POST"},
            {"path": "/internal/backup/import", "tier": "internal", "method": "POST"},
            {"path": "/internal/backup/verify", "tier": "internal", "method": "POST"},

        ],
        "ports": ports or [],  # port -> driver, from the composition root
        # FILL THIS too, or leave it empty deliberately. Same trap as capabilities: an empty
        # `emitted` reads as "this service publishes nothing", so a consumer wiring against it
        # subscribes to nothing and fails silently rather than at boot.
        "events": {"emitted": [], "consumed": []},  # e.g. {"emitted": ["wallet.credited"], ...}
        "admin_actions": _admin_actions(),
        "backup": _backup(),   # what backup-service must capture to restore this service
        "user_actions": _user_actions(),
    }
