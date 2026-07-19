"""onboarding-service — the internal contracts this service CONSUMES.

See services/notification-service/app/service_contracts.py for the format and why this file
exists. safety:contract-callers reads it and refuses a path nobody serves, a port with no code
behind it, and a served contract marked for a service caller that nobody calls.
"""
from __future__ import annotations

from typing import Any

CONSUMES: list[dict[str, Any]] = [
    {
        "service": "identity-service",
        "path": "/internal/identity/facts/{fact}",
        "port": "requirement_source",
        "why": "every flow's first requirement is satisfied_by identity.email_verified, and "
               "identity owns that truth. If this call stops working the requirement evaluates "
               "UNKNOWN — never satisfied — so onboarding stalls visibly instead of handing out "
               "completion grants to people who never proved anything.",
    },
]
