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
     "params": [{"name": "index", "in": "body", "type": "string", "required": True,
                 "help": "Name of the index to search."},
                {"name": "text", "in": "body", "type": "string", "required": False,
                 "help": "The search text. Empty matches everything in the index."},
                {"name": "filters", "in": "body", "type": "object", "required": False,
                 "help": "Field name to required value."},
                {"name": "facets", "in": "body", "type": "array", "required": False,
                 "help": "Field names to return counts for."},
                {"name": "size", "in": "body", "type": "integer", "required": False,
                 "help": "How many hits to return."},
                {"name": "cursor", "in": "body", "type": "string", "required": False,
                 "help": "Cursor from a previous response, to fetch the next page."},
                {"name": "highlight", "in": "body", "type": "boolean", "required": False,
                 "help": "Return matched fragments with the hits."}]},
    {"id": "search.suggest", "label": "Autocomplete suggestions", "method": "GET",
     "path": "/api/v1/search/suggest", "capability": "platform.search", "group": "Search",
     "params": [{"name": "index", "in": "query", "type": "string", "required": True},
                {"name": "prefix", "in": "query", "type": "string", "required": True}]},
    {"id": "search.index_stats", "label": "View index stats", "method": "GET",
     "path": "/api/v1/search/indexes/{index}/stats", "capability": "platform.search", "group": "Indexes",
     "params": [{"name": "index", "in": "path", "type": "string", "required": True}]},
]
