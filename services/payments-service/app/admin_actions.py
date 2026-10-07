"""payments-service — admin actions declared for the platform admin panel (Phase 33).

The panel aggregates what every service declares HERE (it is served on /internal/manifest, so the
code is the source and the panel can never drift). Declaring nothing is fine — a service with an
empty list simply exposes no controls; keep it empty and reviewed rather than forgotten.

Each action is a PURE literal dict (the safety:admin CI gate reads it with ast.literal_eval — no
variables, no f-strings). Shape (docs/contracts/admin-actions.schema.json):

    {
        "id": "svc.do_thing",              # stable, kebab/dot, unique
        "label": "Do the thing",           # what the button says
        "method": "POST",                  # GET|POST|PUT|PATCH|DELETE (allow-listed)
        "path": "/api/v1/things/{id}/do",  # RELATIVE — no scheme/host; must be a real manifest route
        "capability": "platform.svc",      # the cap the route ENFORCES; must be advertised in the manifest
        "params": [{"name": "id", "in": "path", "type": "string", "required": True}],
        "confirm": True,                   # UX: ask before firing (auto-true when danger)
        "danger": True,                    # UX: destructive/irreversible
        "group": "Things",                 # UX: section
    }

Rules the gate enforces: the path must exist in this service's manifest `endpoints`; the capability
must be in the manifest's advertised `capabilities`; the panel is a MAP, never the AUTHORITY — every
action is re-checked at its own route on every call. Only declare OPERATOR (platform.* / *.manage /
*.read) routes; internal-token and per-user `_SELF` routes are never panel actions.
"""
from __future__ import annotations

from typing import Any


ADMIN_ACTIONS: list[dict[str, Any]] = [
    # The operator surface: sealed merchant/gateway credentials and runtime policy, plus the P6
    # per-shop commerce option toggles. Reads are declared too so the panel can show which credential
    # is loaded (a four-char hint; no route ever returns a value — safety:no-secret-reads).
    {
        "id": "operator.list_secrets", "label": "Gateway credentials", "method": "GET",
        "path": "/api/v1/app/operator/secrets", "capability": "platform.secrets",
        "group": "Credentials",
    },
    {
        "id": "operator.set_secret", "label": "Set / rotate a gateway credential", "method": "PUT",
        "path": "/api/v1/app/operator/secrets", "capability": "platform.secrets",
        "group": "Credentials", "confirm": True,
        "params": [
            {"name": "name", "in": "body", "type": "string", "required": True},
            {"name": "value", "in": "body", "type": "string", "required": True,
             "help": "write-only: sealed on arrival, no route can read it back"},
            {"name": "tenant", "in": "body", "type": "string", "required": False,
             "help": "leave empty for platform-wide"},
        ],
    },
    {
        "id": "operator.clear_secret", "label": "Remove a gateway credential", "method": "DELETE",
        "path": "/api/v1/app/operator/secrets/{name}", "capability": "platform.secrets",
        "group": "Credentials", "confirm": True,
        "params": [{"name": "name", "in": "path", "type": "string", "required": True}],
    },
    {
        "id": "operator.get_config", "label": "Runtime settings", "method": "GET",
        "path": "/api/v1/app/operator/config", "capability": "platform.config", "group": "Settings",
    },
    {
        "id": "operator.set_config", "label": "Change a runtime setting", "method": "PUT",
        "path": "/api/v1/app/operator/config", "capability": "platform.config",
        "group": "Settings", "confirm": True,
        "params": [
            {"name": "key", "in": "body", "type": "string", "required": True},
            {"name": "value", "in": "body", "type": "string", "required": True},
            {"name": "tenant", "in": "body", "type": "string", "required": False},
        ],
    },
    {
        "id": "operator.clear_config", "label": "Reset a setting to its default", "method": "DELETE",
        "path": "/api/v1/app/operator/config/{key}", "capability": "platform.config",
        "group": "Settings", "confirm": True,
        "params": [{"name": "key", "in": "path", "type": "string", "required": True}],
    },
    {
        "id": "operator.list_options", "label": "Commerce options (external pay-link)", "method": "GET",
        "path": "/api/v1/app/operator/options", "capability": "platform.config", "group": "Commerce",
    },
    {
        "id": "operator.set_option", "label": "Enable / lock a commerce option", "method": "PUT",
        "path": "/api/v1/app/operator/options", "capability": "platform.config",
        "group": "Commerce", "confirm": True,
        "params": [
            {"name": "option", "in": "body", "type": "string", "required": True,
             "help": "the option to toggle, e.g. external_paylink"},
            {"name": "enabled", "in": "body", "type": "boolean", "required": True},
            {"name": "locked", "in": "body", "type": "boolean", "required": False},
            {"name": "tenant", "in": "body", "type": "string", "required": False,
             "help": "leave empty for a platform-wide (fleet) setting"},
            {"name": "force", "in": "body", "type": "boolean", "required": False,
             "help": "required to change a scope that is already locked"},
        ],
    },
    {
        "id": "operator.clear_option", "label": "Reset a commerce option to its default",
        "method": "DELETE", "path": "/api/v1/app/operator/options/{option}",
        "capability": "platform.config", "group": "Commerce", "confirm": True,
        "params": [{"name": "option", "in": "path", "type": "string", "required": True}],
    },
    {
        "id": "operator.get_payment_mode", "label": "Payment mode (conduit / marketplace)",
        "method": "GET", "path": "/api/v1/app/operator/payment-mode",
        "capability": "platform.config", "group": "Commerce",
        "params": [{"name": "tenant", "in": "query", "type": "string", "required": False,
                    "help": "leave empty for the platform force/default"}],
    },
    {
        "id": "operator.set_payment_mode", "label": "Select / force the payment mode",
        "method": "PUT", "path": "/api/v1/app/operator/payment-mode",
        "capability": "platform.config", "group": "Commerce", "confirm": True,
        "params": [
            {"name": "mode", "in": "body", "type": "string", "required": True,
             "help": "conduit or marketplace"},
            {"name": "tenant", "in": "body", "type": "string", "required": False,
             "help": "leave empty to force/default fleet-wide"},
            {"name": "locked", "in": "body", "type": "boolean", "required": False},
            {"name": "force", "in": "body", "type": "boolean", "required": False,
             "help": "required to change a scope that is already locked"},
        ],
    },
    {
        "id": "operator.view_submerchant", "label": "Sub-merchant onboarding status",
        "method": "GET", "path": "/api/v1/app/operator/sub-merchants/{tenant}",
        "capability": "platform.config", "group": "Commerce",
        "params": [{"name": "tenant", "in": "path", "type": "string", "required": True}],
    },
]
