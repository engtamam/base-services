"""search-service — end-user (self-service) actions: search over the caller's OWN data.

The two read routes (gated by search.read) run a query and offer autocomplete, with HARD tenant
isolation server-side — a user only ever searches their own tenant's documents. `search.read` is the
advertised self-service capability. Index STATS is an operator read (platform.search / search.manage,
in admin_actions), and indexing is internal-token only, so neither is a user action. Each route
re-checks its capability and re-scopes to the caller's tenant on every call.
"""
from __future__ import annotations

from typing import Any


USER_ACTIONS: list[dict[str, Any]] = [
    {"id": "search.query", "label": "Search", "method": "POST",
     "path": "/api/v1/search", "capability": "search.read", "group": "Search",
     "params": [{"name": "index", "in": "body", "type": "string", "required": True},
                {"name": "query", "in": "body", "type": "string", "required": True}]},
    {"id": "search.suggest", "label": "Autocomplete", "method": "GET",
     "path": "/api/v1/search/suggest", "capability": "search.read", "group": "Search",
     "params": [{"name": "index", "in": "query", "type": "string", "required": True},
                {"name": "prefix", "in": "query", "type": "string", "required": True}]},
]
