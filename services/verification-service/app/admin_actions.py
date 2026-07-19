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
]
