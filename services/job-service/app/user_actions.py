"""job-service — end-user (self-service) actions: a developer-customer's OWN jobs.

Background jobs and cron schedules are a developer concern, tenant-scoped from the token. The routes
accept the per-tenant caps jobs.manage (writes) / jobs.read (reads), so the same routes serve a
platform operator (platform.jobs, admin_actions) and a customer's developer (here): view and manage
my schedules, watch my jobs and the dead-letter queue, replay or cancel a job, and read queue stats.
The queue mechanics (enqueue/claim/ack/reclaim/scheduler-tick) are internal-token only, never a user
surface. Each route re-checks its capability and re-scopes to the caller's tenant on every call.
"""
from __future__ import annotations

from typing import Any


USER_ACTIONS: list[dict[str, Any]] = [
    # ── Schedules ────────────────────────────────────────────────────────────
    {"id": "jobs.list_schedules", "label": "My schedules", "method": "GET",
     "path": "/api/v1/jobs/schedules", "capability": "jobs.read", "group": "Schedules"},
    {"id": "jobs.create_schedule", "label": "Create a schedule", "method": "POST",
     "path": "/api/v1/jobs/schedules", "capability": "jobs.manage", "group": "Schedules",
     "confirm": True,
     "params": [{"name": "cron", "in": "body", "type": "string", "required": True},
                {"name": "job_type", "in": "body", "type": "string", "required": True}]},
    {"id": "jobs.delete_schedule", "label": "Delete a schedule", "method": "DELETE",
     "path": "/api/v1/jobs/schedules/{id}", "capability": "jobs.manage", "group": "Schedules",
     "danger": True, "confirm": True,
     "params": [{"name": "id", "in": "path", "type": "string", "required": True}]},
    # ── Jobs ─────────────────────────────────────────────────────────────────
    {"id": "jobs.list", "label": "My jobs", "method": "GET",
     "path": "/api/v1/jobs", "capability": "jobs.read", "group": "Jobs"},
    {"id": "jobs.dead_letters", "label": "Failed jobs", "method": "GET",
     "path": "/api/v1/jobs/dead-letters", "capability": "jobs.read", "group": "Jobs"},
    {"id": "jobs.replay", "label": "Replay a job", "method": "POST",
     "path": "/api/v1/jobs/{id}/replay", "capability": "jobs.manage", "group": "Jobs", "confirm": True,
     "params": [{"name": "id", "in": "path", "type": "string", "required": True}]},
    {"id": "jobs.cancel", "label": "Cancel a job", "method": "POST",
     "path": "/api/v1/jobs/{id}/cancel", "capability": "jobs.manage", "group": "Jobs",
     "danger": True, "confirm": True,
     "params": [{"name": "id", "in": "path", "type": "string", "required": True}]},
    {"id": "jobs.stats", "label": "Queue stats", "method": "GET",
     "path": "/api/v1/jobs/stats", "capability": "jobs.read", "group": "Jobs"},
]
