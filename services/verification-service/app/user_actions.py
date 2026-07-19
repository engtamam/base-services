"""verification-service — user actions (the end-user app).

A person checking their own identity, and nothing else. Every route is scoped to the caller's own
subject: there is no path here that reads another person's verdict, which is why none of these
take a subject parameter.
"""
from __future__ import annotations

from typing import Any

USER_ACTIONS: list[dict[str, Any]] = [
    {
        "id": "verification.submit",
        "label": "Verify my identity",
        "method": "POST",
        "path": "/api/v1/verification/claims",
        "capability": "verification.self",
        "group": "Identity",
        "params": [
            {"name": "claim_type", "in": "body", "type": "string", "required": True,
             "label": "What are you verifying"},
            {"name": "value", "in": "body", "type": "string", "required": True,
             "label": "Identifier", "sensitive": True},
        ],
    },
    {
        "id": "verification.my_claims",
        "label": "My verifications",
        "method": "GET",
        "path": "/api/v1/verification/claims",
        "capability": "verification.self",
        "group": "Identity",
    },
    {
        "id": "verification.my_status",
        "label": "Is my verification current",
        "method": "GET",
        "path": "/api/v1/verification/status",
        "capability": "verification.self",
        "group": "Identity",
    },
]
