"""compliance-service — end-user (self-service) actions.

Compliance is operated by a DPO / compliance officer; almost every route is `_ADMIN`
(platform.compliance / compliance.manage) or `_READ`. The ONE data-subject self-service action is
opening a DSAR about oneself: subject and tenant come from the caller's token, never a body id, so a
subject can exercise their own access/erasure right without being able to name anyone else.

Each action is a PURE literal dict (the safety:user-actions CI gate reads it with ast.literal_eval).
The path must exist in the manifest `endpoints` and the capability must be advertised there. The app
is a MAP, never the AUTHORITY — the route re-checks the capability and re-scopes to the caller.
"""
from __future__ import annotations

from typing import Any


USER_ACTIONS: list[dict[str, Any]] = [
    {
        "id": "compliance.open_my_dsar",
        "label": "Request my data (access / export / erasure / correction)",
        "method": "POST",
        "path": "/api/v1/compliance/me/dsar",
        "capability": "compliance.self",
        "params": [{"name": "type", "in": "body", "type": "string", "required": True}],
        "confirm": True,
        "danger": True,                    # an erasure request is destructive to the subject's data
        "group": "Privacy",
    },
]
