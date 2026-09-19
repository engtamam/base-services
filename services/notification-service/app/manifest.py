# =============================================================================
# notification-service — Self-describing service manifest
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
        # platform.secrets is deliberately separate from platform.config: setting a provider
        # credential and changing a retention period are not the same privilege, and a role that
        # conflates them hands every settings-editor the ability to redirect the platform's mail.
        "capabilities": ["notifications.templates", "notifications.self",
                         "platform.notifications", "platform.secrets", "platform.config"],
        "endpoints": [
            {"path": "/healthz", "tier": "public", "method": "GET"},
            {"path": "/readyz", "tier": "public", "method": "GET"},
            {"path": "/metrics", "tier": "public", "method": "GET"},
            {"path": "/internal/health", "tier": "internal", "method": "GET"},
            {"path": "/internal/whatsapp/flow/{name}-json", "tier": "internal", "method": "GET"},
            {"path": "/api/v1/whatsapp/flows/{name}/preview-json", "tier": "user", "method": "GET"},
            {"path": "/internal/manifest", "tier": "internal", "method": "GET"},
            {"path": "/internal/privacy/erase", "tier": "internal", "method": "POST",
             # DECLARED, because a data map built from manifests is how a DSAR fan-out
             # finds who holds a person. An undeclared holder is never asked, never acks,
             # and is therefore never MISSED — the request closes green on an erasure
             # that did not happen.
             "erasure_category": "subject"},
            {"path": "/internal/notify", "tier": "internal", "method": "POST"},
            {"path": "/internal/notify/whatsapp/interactive", "tier": "internal", "method": "POST"},
            # Send an approved CAROUSEL template with per-card runtime values (media id, body + button
            # params). Business-initiated, consent-gated, through the shared delivery plumbing.
            {"path": "/internal/notify/whatsapp/carousel", "tier": "internal", "method": "POST"},
            {"path": "/api/v1/notifications", "tier": "user", "method": "GET"},
            {"path": "/api/v1/notifications/{id}/read", "tier": "user", "method": "POST"},
            {"path": "/api/v1/notifications/preferences", "tier": "user", "method": "GET"},
            {"path": "/api/v1/notifications/preferences", "tier": "user", "method": "PUT"},
            {"path": "/api/v1/templates", "tier": "user", "method": "POST"},
            {"path": "/api/v1/templates", "tier": "user", "method": "GET"},
            {"path": "/api/v1/whatsapp/templates", "tier": "user", "method": "POST"},
            {"path": "/api/v1/whatsapp/templates", "tier": "user", "method": "GET"},
            {"path": "/api/v1/whatsapp/templates/{name}/{language}/submit", "tier": "user", "method": "POST"},
            {"path": "/api/v1/whatsapp/templates/test-send", "tier": "user", "method": "POST"},
            {"path": "/api/v1/notifications/deliveries", "tier": "user", "method": "GET"},
            # The operator surface: credentials and policy an operator changes after launch.
            # A route table built from manifests that omits where credentials are set hides the
            # highest-privilege surface this service has.
            {"path": "/api/v1/notifications/operator/secrets", "tier": "user", "method": "GET"},
            {"path": "/api/v1/notifications/operator/secrets", "tier": "user", "method": "PUT"},
            {"path": "/api/v1/notifications/operator/secrets/{}", "tier": "user", "method": "DELETE"},
            {"path": "/api/v1/notifications/operator/whatsapp/embedded-signup", "tier": "user", "method": "POST"},
            {"path": "/api/v1/notifications/operator/config", "tier": "user", "method": "GET"},
            {"path": "/api/v1/notifications/operator/config", "tier": "user", "method": "PUT"},
            {"path": "/api/v1/notifications/operator/config/{}", "tier": "user", "method": "DELETE"},
            # Per-shop commerce option toggles (native pay enable/lock) — operator surface (P6).
            {"path": "/api/v1/notifications/operator/options", "tier": "user", "method": "GET"},
            {"path": "/api/v1/notifications/operator/options", "tier": "user", "method": "PUT"},
            {"path": "/api/v1/notifications/operator/options/{}", "tier": "user", "method": "DELETE"},
            {"path": "/internal/whatsapp/embedded-signup", "tier": "internal", "method": "POST"},
            {"path": "/internal/notifications/drain", "tier": "internal", "method": "POST"},
            {"path": "/internal/whatsapp/templates/sync", "tier": "internal", "method": "POST"},
            {"path": "/internal/whatsapp/templates", "tier": "internal", "method": "GET"},
            # Internal WRITE twins of the user-tier register/submit routes: a backend service (an
            # account just connected, a scheduled library upload) is not a user and holds no session.
            {"path": "/internal/whatsapp/templates", "tier": "internal", "method": "POST"},
            {"path": "/internal/whatsapp/templates/{name}/{language}/submit", "tier": "internal", "method": "POST"},
            # Rate-limited drain: submit the next batch of never-submitted templates within Meta's
            # 100/hour cap. Cron this per tenant (like sync) to upload a whole library across ticks.
            {"path": "/internal/whatsapp/templates/submit-pending", "tier": "internal", "method": "POST"},
            # Provision a WABA-scoped Flow (create/clone + publish, idempotent by name) so a template's
            # flow-button can reference it. Called at connect time alongside template registration; a
            # Flow is per-WABA (multi-WABA reuse is a name-matched clone), referenced by flow_NAME.
            {"path": "/internal/whatsapp/flows/provision", "tier": "internal", "method": "POST"},
            # Upload carousel/header media via Meta's Resumable Upload API and return the handle a
            # carousel template needs at creation — so the caller never runs Meta's two-step upload.
            {"path": "/internal/whatsapp/media/upload-handle", "tier": "internal", "method": "POST"},
            # Send-time twin: upload to /{phone_number_id}/media → the media id a carousel card header
            # references at send (distinct from the creation handle above).
            {"path": "/internal/whatsapp/media/upload-id", "tier": "internal", "method": "POST"},
            {"path": "/internal/whatsapp/messaging-limit", "tier": "internal", "method": "GET"},
            # Register a business phone number for Cloud API sending — the PIN-carrying step exchange
            # stops before. Without it a tenant is "connected" everywhere and cannot send. Per NUMBER
            # (a WABA holds many); the PIN is sealed so re-registration repeats without a human.
            {"path": "/internal/whatsapp/register", "tier": "internal", "method": "POST"},
            # A number's real readiness (platform_type, code_verification_status, throughput, quality)
            # read live from Meta — so a shop's state is readable, not assumed.
            {"path": "/internal/whatsapp/status", "tier": "internal", "method": "GET"},
            {"path": "/internal/whatsapp/deliveries", "tier": "internal", "method": "GET"},
            # The catch-up read: delivery/read receipts forward from a cursor (the bus is at-most-once),
            # each carrying delivery_id — so a receipt is matched to the send it belongs to.
            {"path": "/internal/notifications/receipts", "tier": "internal", "method": "GET"},
            {"path": "/internal/whatsapp/inbound", "tier": "internal", "method": "GET"},
            # Inbound Meta webhook (public — auth is the signature / verify token, not a token gate):
            # GET verifies the subscription, POST verifies X-Hub-Signature-256 and fans out every event.
            {"path": "/webhooks/whatsapp", "tier": "public", "method": "GET"},
            {"path": "/webhooks/whatsapp", "tier": "public", "method": "POST"},
            {"path": "/webhooks/whatsapp/flow", "tier": "public", "method": "POST"},
            {"path": "/webhooks/whatsapp/flow/{tenant}", "tier": "public", "method": "POST"},
            {"path": "/webhooks/whatsapp/flow/{tenant}/{handler}", "tier": "public", "method": "POST"},
            # Backup fan-out (backup-service orchestrates): export + verify + import this
            # service's data. Declared so the fan-out discovers this holder — an undeclared holder
            # is one nobody backs up.
            {"path": "/internal/backup/export", "tier": "internal", "method": "POST"},
            {"path": "/internal/backup/import", "tier": "internal", "method": "POST"},
            {"path": "/internal/backup/verify", "tier": "internal", "method": "POST"},
            # Non-secret Embedded-Signup client config (app_id/config_id/graph_version) — never a secret.
            {"path": "/internal/whatsapp/config", "tier": "internal", "method": "GET"},
            # Catalog + product batch sync (commerce P1/P2) — internal peer (a product service) tier.
            {"path": "/internal/whatsapp/catalog", "tier": "internal", "method": "POST"},
            {"path": "/internal/whatsapp/catalogs", "tier": "internal", "method": "GET"},
            {"path": "/internal/whatsapp/catalog/{}", "tier": "internal", "method": "GET"},
            {"path": "/internal/whatsapp/catalog/{}/connect", "tier": "internal", "method": "POST"},
            {"path": "/internal/whatsapp/catalog/{}/products", "tier": "internal", "method": "POST"},
            {"path": "/internal/whatsapp/commerce-settings", "tier": "internal", "method": "POST"},
            # Native in-WhatsApp pay seam (commerce P5, KSA-dormant) — internal peer tier.
            {"path": "/internal/whatsapp/pay/order-details", "tier": "internal", "method": "POST"},
            {"path": "/internal/whatsapp/pay/order-status", "tier": "internal", "method": "POST"},
            {"path": "/internal/whatsapp/pay/lookup", "tier": "internal", "method": "POST"},
            {"path": "/internal/whatsapp/pay/refund", "tier": "internal", "method": "POST"},
            # WhatsApp Coexistence catch-up reads (owner-app echoes/history/contacts) + retention purge.
            {"path": "/internal/whatsapp/coexistence/echoes", "tier": "internal", "method": "GET"},
            {"path": "/internal/whatsapp/coexistence/history", "tier": "internal", "method": "GET"},
            {"path": "/internal/whatsapp/coexistence/contacts", "tier": "internal", "method": "GET"},
            {"path": "/internal/whatsapp/coexistence/purge", "tier": "internal", "method": "POST"},
            {"path": "/internal/whatsapp/inbound/purge", "tier": "internal", "method": "POST"},
            # Ensure the WABA's Flow data-endpoint encryption keypair (generate/register, idempotent).
            {"path": "/internal/whatsapp/flow-encryption/ensure", "tier": "internal", "method": "POST"},
            # Operator platform-status glance — bounded AGGREGATE counts only (no rows, no PII).
            {"path": "/internal/whatsapp/platform-status", "tier": "internal", "method": "GET"},
            # Set/rotate a number's 6-digit two-step-verification PIN (write-only; the PIN is never read
            # back). Distinct from /register, which activates sending using the PIN.
            {"path": "/internal/whatsapp/two-step-pin", "tier": "internal", "method": "POST"},
            # Read-only operator confirmations, live from Meta: which apps/fields are subscribed to the
            # WABA's webhooks, and the WABA's business-verification status.
            {"path": "/internal/whatsapp/subscribed-fields", "tier": "internal", "method": "GET"},
            {"path": "/internal/whatsapp/business-verification", "tier": "internal", "method": "GET"},

        ],
        "ports": ports or [],  # port -> driver, from the composition root
        "events": {"emitted": [], "consumed": []},
        "admin_actions": _admin_actions(),
        "backup": _backup(),   # what backup-service must capture to restore this service
        "user_actions": _user_actions(),
    }
