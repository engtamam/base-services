"""verification-service — admin actions (the operator panel).

Only the review queue. Everything else this service does is a user acting on their own claim, and
an operator does not need — or get — a way to browse other people's identities.

Deliberately absent: any route that takes a raw national id or commercial registration. An admin
panel that could look up "is 1010123456 verified?" would be an enumeration oracle over the whole
country's registry, usable by anyone who compromises one operator account. The `seen` check takes a
claim_ref, so the caller must already hold the hash.
"""
from __future__ import annotations

from typing import Any

ADMIN_ACTIONS: list[dict[str, Any]] = [
    {
        "id": "verification.review_queue",
        "label": "Review queue",
        "method": "GET",
        "path": "/api/v1/verification/review-queue",
        "capability": "verification.review",
        "group": "Verification",
    },
    {
        "id": "verification.decide",
        "label": "Approve or refuse a claim",
        "method": "POST",
        "path": "/api/v1/verification/review/{verification_id}",
        "capability": "verification.review",
        "group": "Verification",
        "confirm": True,
        "params": [
            {"name": "verification_id", "in": "path", "type": "string", "required": True,
             "label": "Verification"},
            {"name": "approve", "in": "body", "type": "boolean", "required": True,
             "label": "Approve"},
            {"name": "note", "in": "body", "type": "string", "required": False,
             "label": "Reviewer note"},
        ],
    },
    # ── Operator controls: credentials and policy, changeable after launch ────
    # A panel that can only ACT is half a panel: the reads are declared too, so an operator can
    # see which credential is loaded before replacing it. No action here returns a value — the
    # list shows a four-character hint, and safety:no-secret-reads fails the build if that
    # ever changes.
    {
        "id": "operator.list_secrets", "label": "Provider credentials", "method": "GET",
        "path": "/api/v1/verification/operator/secrets", "capability": "platform.secrets",
        "group": "Credentials",
    },
    {
        "id": "operator.set_secret", "label": "Set / rotate a provider credential", "method": "PUT",
        "path": "/api/v1/verification/operator/secrets", "capability": "platform.secrets",
        "group": "Credentials", "confirm": True,
        "params": [
            {"name": "name", "in": "body", "type": "string", "required": True,
             "help": "the setting it replaces, the secret NAME from VERIFY_SECRETS, e.g. WATHQ_TOKEN"},
            {"name": "value", "in": "body", "type": "string", "required": True,
             "help": "write-only: it is sealed on arrival and no route can read it back"},
            {"name": "tenant", "in": "body", "type": "string", "required": False,
             "help": "leave empty for platform-wide"},
        ],
    },
    {
        "id": "operator.clear_secret", "label": "Remove a provider credential", "method": "DELETE",
        "path": "/api/v1/verification/operator/secrets/{name}", "capability": "platform.secrets",
        "group": "Credentials", "confirm": True,
        "params": [{"name": "name", "in": "path", "type": "string", "required": True}],
    },
    {
        "id": "operator.get_config", "label": "Runtime settings", "method": "GET",
        "path": "/api/v1/verification/operator/config", "capability": "platform.config", "group": "Settings",
    },
    {
        "id": "operator.set_config", "label": "Change a runtime setting", "method": "PUT",
        "path": "/api/v1/verification/operator/config", "capability": "platform.config",
        "group": "Settings", "confirm": True,
        "params": [
            {"name": "key", "in": "body", "type": "string", "required": True,
             "help": "e.g. VERIFY_PROVIDERS to repoint a claim type at another provider"},
            {"name": "value", "in": "body", "type": "string", "required": True},
            {"name": "tenant", "in": "body", "type": "string", "required": False},
        ],
    },
    {
        "id": "operator.clear_config", "label": "Reset a setting to its default", "method": "DELETE",
        "path": "/api/v1/verification/operator/config/{key}", "capability": "platform.config",
        "group": "Settings", "confirm": True,
        "params": [{"name": "key", "in": "path", "type": "string", "required": True}],
    },
]
