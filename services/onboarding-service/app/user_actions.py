"""onboarding-service — user actions (the end-user app).

Both actions are scoped to the caller: there is no route here that reads another person's flow,
which is why neither takes a subject.
"""
from __future__ import annotations

from typing import Any

USER_ACTIONS: list[dict[str, Any]] = [
    {
        "id": "onboarding.next",
        "label": "What's next for me",
        "method": "POST",
        "path": "/api/v1/onboarding/next",
        "capability": "onboarding.self",
        "group": "Getting started",
    },
    {
        "id": "onboarding.provision",
        "label": "Set up my account",
        "method": "POST",
        "path": "/api/v1/onboarding/provision",
        "capability": "onboarding.self",
        "group": "Getting started",
    },
    {
        "id": "onboarding.claim_grant",
        "label": "Claim my welcome grant",
        "method": "POST",
        "path": "/api/v1/onboarding/claim-grant",
        "capability": "onboarding.self",
        "group": "Getting started",
    },
]
