"""onboarding-service — admin actions (the operator panel).

Support tools for customers self-serve cannot unstick. `force` is deliberately labelled for what it
does rather than as "fix onboarding": an operator should feel the weight of overriding a check, and
the panel should not disguise it.
"""
from __future__ import annotations

from typing import Any

ADMIN_ACTIONS: list[dict[str, Any]] = [
    {
        "id": "onboarding.inspect",
        "label": "Inspect a customer's onboarding",
        "method": "POST",
        "path": "/api/v1/onboarding/admin/inspect",
        "capability": "onboarding.manage",
        "group": "Onboarding",
        "params": [{"name": "subject", "in": "query", "type": "string", "required": True,
                    "label": "Subject"}],
    },
    {
        "id": "onboarding.force",
        "label": "Override a requirement (grants trust by hand)",
        "method": "POST",
        "path": "/api/v1/onboarding/admin/force/{subject}",
        "capability": "onboarding.manage",
        "group": "Onboarding",
        "confirm": True,
        "params": [
            {"name": "subject", "in": "path", "type": "string", "required": True,
             "label": "Subject"},
            {"name": "requirement", "in": "body", "type": "string", "required": True,
             "label": "Requirement to override"},
            {"name": "reason", "in": "body", "type": "string", "required": True,
             "label": "Why (recorded in the audit log)"},
        ],
    },
    # The see-before-you-act reads, and the one unstick action self-serve cannot reach.
    {
        "id": "onboarding.flows",
        "label": "Onboarding flows",
        "method": "GET",
        "path": "/api/v1/onboarding/admin/flows",
        "capability": "onboarding.manage",
        "group": "Onboarding",
    },
    {
        "id": "onboarding.provisioning",
        "label": "A customer's provisioning run",
        "method": "GET",
        "path": "/api/v1/onboarding/admin/provisioning/{subject}",
        "capability": "onboarding.manage",
        "group": "Onboarding",
        "params": [{"name": "subject", "in": "path", "type": "string", "required": True,
                    "label": "Subject"}],
    },
    {
        "id": "onboarding.resume",
        "label": "Resume stuck provisioning cleanup",
        "method": "POST",
        "path": "/api/v1/onboarding/admin/resume/{subject}",
        "capability": "onboarding.manage",
        "group": "Onboarding",
        "confirm": True,
        "params": [{"name": "subject", "in": "path", "type": "string", "required": True,
                    "label": "Subject"}],
    },
]
