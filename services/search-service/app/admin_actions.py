"""search-service — admin actions (query + index stats).

Indexing only ever happens through internal-token routes (/internal/search/index | bulk | delete),
so an operator can never mutate the index from the panel — the panel can QUERY and inspect, never
write. The three user-tier routes are all capability-gated (platform.search / search.read for query
and suggest, platform.search / search.manage for index stats). Every action re-checks its capability
at the route; this file only decides what is shown.
"""
from __future__ import annotations

from typing import Any


ADMIN_ACTIONS: list[dict[str, Any]] = [
    {"id": "search.query", "label": "Run a search", "method": "POST",
     "path": "/api/v1/search", "capability": "platform.search", "group": "Search",
     "params": [{"name": "index", "in": "body", "type": "string", "required": True},
                {"name": "query", "in": "body", "type": "string", "required": True}]},
    {"id": "search.suggest", "label": "Autocomplete suggestions", "method": "GET",
     "path": "/api/v1/search/suggest", "capability": "platform.search", "group": "Search",
     "params": [{"name": "index", "in": "query", "type": "string", "required": True},
                {"name": "prefix", "in": "query", "type": "string", "required": True}]},
    {"id": "search.index_stats", "label": "View index stats", "method": "GET",
     "path": "/api/v1/search/indexes/{index}/stats", "capability": "platform.search", "group": "Indexes",
     "params": [{"name": "index", "in": "path", "type": "string", "required": True}]},
]
