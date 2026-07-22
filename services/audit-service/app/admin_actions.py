"""audit-service — admin actions (integrity verify + retention purge).

The two tenant-scoped routes — query and export — run under require_user (a person reads their OWN
trail, filtered from the token), so they are `_SELF`, not operator actions. The operator surface is
the two platform.audit routes:
  - verify: recompute the hash chain and report whether the log has been tampered with (read-only).
  - purge: enforce retention by deleting events before a cutoff. It is the single most sensitive
    action in the platform — it destroys evidence — so it is danger + confirm and bounded by
    before_ts. There is deliberately no "purge all" without a cutoff. Append is internal-token only,
    so an operator can never forge an event from the panel; they can verify and time-bound-purge.
Every action re-checks platform.audit at the route; this file only decides what is shown.
"""
from __future__ import annotations

from typing import Any


ADMIN_ACTIONS: list[dict[str, Any]] = [
    {
        "id": "audit.query", "label": "Search the audit log", "method": "GET",
        "path": "/api/v1/audit", "capability": "platform.audit", "group": "Audit",
        # An audit log that cannot be narrowed by actor or action is unusable at the size an audit
        # log reaches. These filters always worked; they were read off the raw query string and so
        # never reached the spec, which made them invisible to this catalog and to any generated
        # client. They are declared on the route now.
        "params": [
            {"name": "actor", "in": "query", "type": "string", "required": False,
             "help": "a subject pseudonym, never a name"},
            {"name": "action", "in": "query", "type": "string", "required": False},
            {"name": "service", "in": "query", "type": "string", "required": False},
            {"name": "resource", "in": "query", "type": "string", "required": False},
            {"name": "since", "in": "query", "type": "string", "required": False},
            {"name": "until", "in": "query", "type": "string", "required": False},
            {"name": "tenant", "in": "query", "type": "string", "required": False,
             "help": "platform.audit only; a tenant admin is always scoped to their own"},
            {"name": "limit", "in": "query", "type": "integer", "required": False},
        ],
    },
    {
        "id": "audit.verify", "label": "Verify log integrity (hash chain)", "method": "GET",
        "path": "/api/v1/audit/verify", "capability": "platform.audit", "group": "Audit",
        "params": [{"name": "partition", "in": "query", "type": "string", "required": False,
                    "help": "defaults to the global partition"}],
    },
    {
        "id": "audit.purge", "label": "Purge events before a cutoff (retention)", "method": "POST",
        "path": "/api/v1/audit/purge", "capability": "platform.audit", "group": "Audit",
        "danger": True, "confirm": True,
        "params": [
            {"name": "before_ts", "in": "body", "type": "string", "required": True,
             "help": "delete events strictly before this timestamp — destroys evidence, irreversible"},
            {"name": "partition", "in": "body", "type": "string", "required": False},
        ],
    },
]
