"""job-service — admin actions (the complete operator surface).

The whole queue mechanism — enqueue, claim, ack, nack, heartbeat, reclaim, scheduler tick — is
internal-token, so an operator never hand-drives a worker from the panel. The user-tier routes are
all operator-scoped (platform.jobs / jobs.manage for mutations, jobs.read for the views): manage
cron schedules, list jobs and the dead-letter queue, replay or cancel a job, and read queue stats.
Deleting a schedule and cancelling a job are danger + confirm. Every action re-checks its capability
at the route; this file only decides what is shown.
"""
from __future__ import annotations

from typing import Any


ADMIN_ACTIONS: list[dict[str, Any]] = [
    # ── Schedules ────────────────────────────────────────────────────────────
    {"id": "jobs.list_schedules", "label": "List schedules", "method": "GET",
     "path": "/api/v1/jobs/schedules", "capability": "platform.jobs", "group": "Schedules"},
    {"id": "jobs.create_schedule", "label": "Create a schedule", "method": "POST",
     "path": "/api/v1/jobs/schedules", "capability": "platform.jobs", "group": "Schedules",
     "confirm": True,
     "params": [{"name": "cron", "in": "body", "type": "string", "required": True},
                {"name": "job_type", "in": "body", "type": "string", "required": True}]},
    {"id": "jobs.delete_schedule", "label": "Delete a schedule", "method": "DELETE",
     "path": "/api/v1/jobs/schedules/{id}", "capability": "platform.jobs", "group": "Schedules",
     "danger": True, "confirm": True,
     "params": [{"name": "id", "in": "path", "type": "string", "required": True}]},
    # ── Jobs ─────────────────────────────────────────────────────────────────
    {"id": "jobs.list", "label": "List jobs", "method": "GET",
     "path": "/api/v1/jobs", "capability": "platform.jobs", "group": "Jobs"},
    {"id": "jobs.dead_letters", "label": "List dead-letters", "method": "GET",
     "path": "/api/v1/jobs/dead-letters", "capability": "platform.jobs", "group": "Jobs"},
    {"id": "jobs.replay", "label": "Replay a job", "method": "POST",
     "path": "/api/v1/jobs/{id}/replay", "capability": "platform.jobs", "group": "Jobs", "confirm": True,
     "params": [{"name": "id", "in": "path", "type": "string", "required": True}]},
    {"id": "jobs.cancel", "label": "Cancel a job", "method": "POST",
     "path": "/api/v1/jobs/{id}/cancel", "capability": "platform.jobs", "group": "Jobs",
     "danger": True, "confirm": True,
     "params": [{"name": "id", "in": "path", "type": "string", "required": True}]},
    {"id": "jobs.stats", "label": "View queue stats", "method": "GET",
     "path": "/api/v1/jobs/stats", "capability": "platform.jobs", "group": "Jobs"},
]
