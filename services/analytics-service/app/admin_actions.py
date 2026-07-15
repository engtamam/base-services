"""analytics-service — admin actions (the complete operator surface).

Event ingestion and raw-pruning are internal-token (/internal/analytics/*), so an operator never
writes data from the panel — the panel only ASKS questions of it. Every user-tier route is
operator-scoped (platform.analytics / analytics.read for queries and report reads, analytics.manage
for creating or deleting a saved report): ad-hoc query / funnel / retention, saved-report CRUD and
run, and export. Deleting a report is danger + confirm. Every action re-checks its capability at the
route; this file only decides what is shown.
"""
from __future__ import annotations

from typing import Any


ADMIN_ACTIONS: list[dict[str, Any]] = [
    # ── Ad-hoc analysis ──────────────────────────────────────────────────────
    {"id": "analytics.query", "label": "Run a query", "method": "POST",
     "path": "/api/v1/analytics/query", "capability": "platform.analytics", "group": "Analysis"},
    {"id": "analytics.funnel", "label": "Run a funnel", "method": "POST",
     "path": "/api/v1/analytics/funnel", "capability": "platform.analytics", "group": "Analysis"},
    {"id": "analytics.retention", "label": "Run a retention analysis", "method": "POST",
     "path": "/api/v1/analytics/retention", "capability": "platform.analytics", "group": "Analysis"},
    {"id": "analytics.export", "label": "Export a query result", "method": "POST",
     "path": "/api/v1/analytics/export", "capability": "platform.analytics", "group": "Analysis"},
    # ── Saved reports ────────────────────────────────────────────────────────
    {"id": "analytics.list_reports", "label": "List saved reports", "method": "GET",
     "path": "/api/v1/analytics/reports", "capability": "platform.analytics", "group": "Reports"},
    {"id": "analytics.create_report", "label": "Create a saved report", "method": "POST",
     "path": "/api/v1/analytics/reports", "capability": "platform.analytics", "group": "Reports",
     "confirm": True},
    {"id": "analytics.get_report", "label": "View a saved report", "method": "GET",
     "path": "/api/v1/analytics/reports/{report_id}", "capability": "platform.analytics",
     "group": "Reports",
     "params": [{"name": "report_id", "in": "path", "type": "string", "required": True}]},
    {"id": "analytics.run_report", "label": "Run a saved report", "method": "POST",
     "path": "/api/v1/analytics/reports/{report_id}/run", "capability": "platform.analytics",
     "group": "Reports",
     "params": [{"name": "report_id", "in": "path", "type": "string", "required": True}]},
    {"id": "analytics.delete_report", "label": "Delete a saved report", "method": "DELETE",
     "path": "/api/v1/analytics/reports/{report_id}", "capability": "platform.analytics",
     "group": "Reports", "danger": True, "confirm": True,
     "params": [{"name": "report_id", "in": "path", "type": "string", "required": True}]},
]
