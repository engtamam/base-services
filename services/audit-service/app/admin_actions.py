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
        # THE READ AN OPERATOR MOST NEEDS, and it was the one the panel could not render.
        # GET /api/v1/audit answers ACROSS tenants for a platform.audit holder, but it was
        # declared only in user_actions as the self-service "my activity log". A panel builds
        # itself from the ADMIN catalog, so an investigator had a purge button and a verify
        # button and no way to look at the log.
        "id": "audit.query", "label": "Search the audit log", "method": "GET",
        "path": "/api/v1/audit", "capability": "platform.audit", "group": "Audit",
        "params": [
            {"name": "tenant", "in": "query", "type": "string", "required": False,
             "help": "omit to search ACROSS tenants — platform.audit only"},
            {"name": "actor", "in": "query", "type": "string", "required": False,
             "help": "a raw identifier is folded to its pseudonym before matching"},
            {"name": "action", "in": "query", "type": "string", "required": False},
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
