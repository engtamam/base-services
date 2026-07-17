"""stream-service — end-user (self-service) actions: a customer's OWN cameras.

Every stream route is tenant-scoped (the tenant comes from the token), so the SAME routes are both an
operator surface (platform.stream, in admin_actions) and a customer's own self-service. Here they are
the customer's: manage my cameras, watch a live session, drive PTZ, and review my recordings. Reads
use `stream.view`, mutations use `stream.manage` (both advertised, both accepted by the routes'
require_any_capability guards). Actual media never flows through the API. Each route re-checks its
capability AND re-scopes to the caller's tenant on every call; this file only decides what the app
shows. Paths use the manifest's `{id}`/`{token}` placeholders.
"""
from __future__ import annotations

from typing import Any


USER_ACTIONS: list[dict[str, Any]] = [
    # ── Cameras ──────────────────────────────────────────────────────────────
    {"id": "stream.list_sources", "label": "My cameras", "method": "GET",
     "path": "/api/v1/sources", "capability": "stream.view", "group": "Cameras"},
    {"id": "stream.register_source", "label": "Add a camera", "method": "POST",
     "path": "/api/v1/sources", "capability": "stream.manage", "group": "Cameras", "confirm": True,
     "params": [{"name": "name", "in": "body", "type": "string", "required": True},
                {"name": "url", "in": "body", "type": "string", "required": True}]},
    {"id": "stream.get_source", "label": "View a camera", "method": "GET",
     "path": "/api/v1/sources/{id}", "capability": "stream.view", "group": "Cameras",
     "params": [{"name": "id", "in": "path", "type": "string", "required": True}]},
    {"id": "stream.probe_source", "label": "Test a camera", "method": "POST",
     "path": "/api/v1/sources/{id}/probe", "capability": "stream.view", "group": "Cameras",
     "params": [{"name": "id", "in": "path", "type": "string", "required": True}]},
    {"id": "stream.delete_source", "label": "Remove a camera", "method": "DELETE",
     "path": "/api/v1/sources/{id}", "capability": "stream.manage", "group": "Cameras",
     "danger": True, "confirm": True,
     "params": [{"name": "id", "in": "path", "type": "string", "required": True}]},
    {"id": "stream.get_triggers", "label": "View triggers", "method": "GET",
     "path": "/api/v1/sources/{id}/triggers", "capability": "stream.view", "group": "Cameras",
     "params": [{"name": "id", "in": "path", "type": "string", "required": True}]},
    {"id": "stream.set_triggers", "label": "Set triggers (motion → record/notify)", "method": "PUT",
     "path": "/api/v1/sources/{id}/triggers", "capability": "stream.manage", "group": "Cameras",
     "confirm": True,
     "params": [{"name": "id", "in": "path", "type": "string", "required": True}]},
    # ── Live sessions ────────────────────────────────────────────────────────
    {"id": "stream.list_sessions", "label": "My live sessions", "method": "GET",
     "path": "/api/v1/sessions", "capability": "stream.view", "group": "Live"},
    {"id": "stream.start_session", "label": "Start watching", "method": "POST",
     "path": "/api/v1/sessions", "capability": "stream.manage", "group": "Live", "confirm": True,
     "params": [{"name": "source_id", "in": "body", "type": "string", "required": True}]},
    {"id": "stream.get_session", "label": "Session status", "method": "GET",
     "path": "/api/v1/sessions/{id}", "capability": "stream.view", "group": "Live",
     "params": [{"name": "id", "in": "path", "type": "string", "required": True}]},
    {"id": "stream.playback", "label": "Watch (playback handles)", "method": "GET",
     "path": "/api/v1/sessions/{id}/playback", "capability": "stream.view", "group": "Live",
     "params": [{"name": "id", "in": "path", "type": "string", "required": True}]},
    {"id": "stream.stop_session", "label": "Stop watching", "method": "POST",
     "path": "/api/v1/sessions/{id}/stop", "capability": "stream.manage", "group": "Live",
     "confirm": True,
     "params": [{"name": "id", "in": "path", "type": "string", "required": True}]},
    # ── PTZ ──────────────────────────────────────────────────────────────────
    {"id": "stream.ptz_move", "label": "Move the camera", "method": "POST",
     "path": "/api/v1/sources/{id}/ptz/move", "capability": "stream.manage", "group": "PTZ",
     "params": [{"name": "id", "in": "path", "type": "string", "required": True},
                {"name": "pan", "in": "body", "type": "number", "required": False},
                {"name": "tilt", "in": "body", "type": "number", "required": False},
                {"name": "zoom", "in": "body", "type": "number", "required": False}]},
    {"id": "stream.ptz_stop", "label": "Stop moving", "method": "POST",
     "path": "/api/v1/sources/{id}/ptz/stop", "capability": "stream.manage", "group": "PTZ",
     "params": [{"name": "id", "in": "path", "type": "string", "required": True}]},
    {"id": "stream.ptz_presets", "label": "PTZ presets", "method": "GET",
     "path": "/api/v1/sources/{id}/ptz/presets", "capability": "stream.view", "group": "PTZ",
     "params": [{"name": "id", "in": "path", "type": "string", "required": True}]},
    {"id": "stream.ptz_goto", "label": "Go to preset", "method": "POST",
     "path": "/api/v1/sources/{id}/ptz/goto/{token}", "capability": "stream.manage", "group": "PTZ",
     "params": [{"name": "id", "in": "path", "type": "string", "required": True},
                {"name": "token", "in": "path", "type": "string", "required": True}]},
    # ── Recordings ───────────────────────────────────────────────────────────
    {"id": "stream.list_recordings", "label": "My recordings", "method": "GET",
     "path": "/api/v1/recordings", "capability": "stream.view", "group": "Recordings"},
    {"id": "stream.get_recording", "label": "View a recording", "method": "GET",
     "path": "/api/v1/recordings/{id}", "capability": "stream.view", "group": "Recordings",
     "params": [{"name": "id", "in": "path", "type": "string", "required": True}]},
    {"id": "stream.delete_recording", "label": "Delete a recording", "method": "DELETE",
     "path": "/api/v1/recordings/{id}", "capability": "stream.manage", "group": "Recordings",
     "danger": True, "confirm": True,
     "params": [{"name": "id", "in": "path", "type": "string", "required": True}]},
]
