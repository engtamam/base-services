# =============================================================================
# identity-service — Self-describing service manifest
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


def build_manifest(
    settings: Settings, ports: list[dict[str, str]] | None = None
) -> dict[str, Any]:
    return {
        "name": settings.SERVICE_NAME,
        "version": settings.SERVICE_VERSION,
        "env": settings.ENV,
        # `members:read`/`members:write` guard the multi-tenant directory (Phase 31). Advertised
        # here so the admin panel can gate its member actions on them — the cap NAMES default to
        # these and are overridable via IDENTITY_MEMBERS_READ_CAP / _WRITE_CAP.
        "capabilities": ["identity.register", "identity.login", "identity.refresh",
                         "identity.self", "members:read", "members:write",
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
            {"path": "/api/v1/auth/register", "tier": "user", "method": "POST"},
            {"path": "/api/v1/auth/login", "tier": "user", "method": "POST"},
            {"path": "/api/v1/auth/verify-email", "tier": "user", "method": "POST"},
            {"path": "/api/v1/auth/resend-verification", "tier": "user", "method": "POST"},
            {"path": "/api/v1/auth/refresh", "tier": "user", "method": "POST"},
            {"path": "/api/v1/auth/logout", "tier": "user", "method": "POST"},
            {"path": "/api/v1/auth/me", "tier": "user", "method": "GET"},
            {"path": "/api/v1/auth/me", "tier": "user", "method": "DELETE"},
            {"path": "/api/v1/auth/change-password", "tier": "user", "method": "POST"},
            {"path": "/api/v1/auth/mfa/enroll", "tier": "user", "method": "POST"},
            {"path": "/api/v1/auth/mfa/verify", "tier": "user", "method": "POST"},
            # Multi-tenant directory (Phase 31) — mounted when IDENTITY_MULTI_TENANT is on.
            {"path": "/api/v1/tenancy/members", "tier": "user", "method": "GET"},
            {"path": "/api/v1/tenancy/members", "tier": "user", "method": "POST"},
            {"path": "/api/v1/tenancy/members/{person_id}", "tier": "user", "method": "PUT"},
            {"path": "/api/v1/tenancy/members/{person_id}", "tier": "user", "method": "DELETE"},
            {"path": "/internal/identity/facts/{fact}", "tier": "internal", "method": "GET",
             # onboarding declares identity as the OWNER of these facts; it must be able to ask.
             "caller": "service"},
            {"path": "/internal/tenancy/members", "tier": "internal", "method": "GET"},
            {"path": "/internal/tenancy/seed-owner", "tier": "internal", "method": "POST"},
            {"path": "/internal/tenancy/membership/{ref}", "tier": "internal", "method": "DELETE"},
            {"path": "/internal/ratelimit/unblock", "tier": "internal", "method": "POST"},
            {"path": "/api/v1/auth/person/claim", "tier": "user", "method": "POST"},
            {"path": "/api/v1/auth/person/login", "tier": "user", "method": "POST"},
            {"path": "/api/v1/auth/tenant", "tier": "user", "method": "POST"},
            # The operator surface: credentials and policy an operator changes after launch.
            {"path": "/api/v1/operator/secrets", "tier": "user", "method": "GET"},
            {"path": "/api/v1/operator/secrets", "tier": "user", "method": "PUT"},
            {"path": "/api/v1/operator/secrets/{}", "tier": "user", "method": "DELETE"},
            {"path": "/api/v1/operator/config", "tier": "user", "method": "GET"},
            {"path": "/api/v1/operator/config", "tier": "user", "method": "PUT"},
            {"path": "/api/v1/operator/config/{}", "tier": "user", "method": "DELETE"},
        ],
        "ports": ports or [],  # port -> driver, from the composition root
        # tenant.owner_seeded — a tenant's first owner was bootstrapped as a Person. At-most-once
        # HINT: the durable fact is the membership row + the audit, never this event. A consumer
        # (notification welcome, billing trial-start) resolves the human from person_id, since no
        # raw identifier travels on the bus. Declared so a consumer can discover it exists.
        "events": {"emitted": ["tenant.owner_seeded"], "consumed": []},
        "admin_actions": _admin_actions(),
        "user_actions": _user_actions(),
    }
