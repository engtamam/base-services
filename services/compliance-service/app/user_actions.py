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
    {
        "id": "compliance.accept_legal",
        "label": "Accept the current terms",
        "method": "POST",
        "path": "/api/v1/compliance/me/legal/{doc_id}/accept",
        "capability": "compliance.self",
        "params": [{"name": "doc_id", "in": "path", "type": "string", "required": True}],
        "group": "Legal",
    },
]

# Routes deliberately NOT rendered as app controls. Machine-checked by safety:action-coverage: every
# user-tier route must be declared above, in admin_actions, or excluded here WITH a reason.
UNDECLARED_ROUTES: list[dict[str, str]] = [
    {"method": "GET", "path": "/api/v1/compliance/legal",
     "why": "content the app RENDERS (the list of legal documents it must show/gate on), not a "
            "button a user clicks — the shell reads it to know what to display, like a page fetch"},
    {"method": "GET", "path": "/api/v1/compliance/legal/{doc_id}",
     "why": "the structured legal-page viewer payload — the terms are shown inline for reading and "
            "acceptance, so this is a content fetch, not a catalog action"},
    {"method": "GET", "path": "/api/v1/compliance/me/legal/outstanding",
     "why": "the app shell calls this automatically right after login to decide whether to block on "
            "a re-acceptance — a gate check the shell drives, not a button a user clicks"},
]
