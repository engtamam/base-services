"""audit-service — the internal contracts this service CONSUMES.

See services/notification-service/app/service_contracts.py for the format and why this file
exists. safety:contract-callers reads it and refuses a path nobody serves, a port with no code
behind it, and — on the producing side — a contract marked for a service caller that nobody calls.
"""
from __future__ import annotations

from typing import Any

CONSUMES: list[dict[str, Any]] = [
    {
        "service": "compliance-service",
        "path": "/internal/compliance/held-refs",
        "port": "hold_refs",
        "why": "retention purge must not delete events belonging to a subject under legal hold. "
               "This service cannot use per-subject hold-check — its rows carry a pseudonymous "
               "subject_ref with no inverse — so it fetches the held refs per partition and "
               "intersects locally. If this call stops working the purge SKIPS that partition "
               "and says so; it never deletes on an unknown or partial hold list.",
    },
]
