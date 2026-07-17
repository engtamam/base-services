"""analytics-service — end-user (self-service) actions: analyse the caller's OWN data.

Running a query/funnel/retention, exporting a result, and saving/running/deleting a report are the
analyst's self-service — every route is tenant-scoped from the token, so a user only ever analyses
their own tenant's events. The routes accept the per-tenant caps analytics.read (queries + report
reads) / analytics.manage (create/delete a saved report), so the same routes serve a platform operator
(platform.analytics, admin_actions) and a customer's analyst (here). Event ingestion and raw-pruning
are internal-token only, never a user surface. Each route re-checks its capability and re-scopes to
the caller's tenant on every call.
"""
from __future__ import annotations

from typing import Any


USER_ACTIONS: list[dict[str, Any]] = [
    # ── Ad-hoc analysis ──────────────────────────────────────────────────────
    {"id": "analytics.query", "label": "Run a query", "method": "POST",
     "path": "/api/v1/analytics/query", "capability": "analytics.read", "group": "Analysis"},
    {"id": "analytics.funnel", "label": "Run a funnel", "method": "POST",
     "path": "/api/v1/analytics/funnel", "capability": "analytics.read", "group": "Analysis"},
    {"id": "analytics.retention", "label": "Run a retention analysis", "method": "POST",
     "path": "/api/v1/analytics/retention", "capability": "analytics.read", "group": "Analysis"},
    {"id": "analytics.export", "label": "Export a result", "method": "POST",
     "path": "/api/v1/analytics/export", "capability": "analytics.read", "group": "Analysis"},
    # ── Saved reports ────────────────────────────────────────────────────────
    {"id": "analytics.list_reports", "label": "My reports", "method": "GET",
     "path": "/api/v1/analytics/reports", "capability": "analytics.read", "group": "Reports"},
    {"id": "analytics.create_report", "label": "Save a report", "method": "POST",
     "path": "/api/v1/analytics/reports", "capability": "analytics.manage", "group": "Reports",
     "confirm": True},
    {"id": "analytics.get_report", "label": "View a report", "method": "GET",
     "path": "/api/v1/analytics/reports/{id}", "capability": "analytics.read", "group": "Reports",
     "params": [{"name": "id", "in": "path", "type": "string", "required": True}]},
    {"id": "analytics.run_report", "label": "Run a report", "method": "POST",
     "path": "/api/v1/analytics/reports/{id}/run", "capability": "analytics.read", "group": "Reports",
     "params": [{"name": "id", "in": "path", "type": "string", "required": True}]},
    {"id": "analytics.delete_report", "label": "Delete a report", "method": "DELETE",
     "path": "/api/v1/analytics/reports/{id}", "capability": "analytics.manage", "group": "Reports",
     "danger": True, "confirm": True,
     "params": [{"name": "id", "in": "path", "type": "string", "required": True}]},
]
