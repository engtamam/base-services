"""stream-service — admin actions (the operator surface for live cameras).

Every user-tier route is operator-scoped (platform.stream / stream.manage for mutations,
stream.view for reads); there is no `_SELF` route. So the whole surface is the panel's: manage camera
sources, drive live sessions (start/stop — a session can never run forever thanks to the guards),
inspect recordings and place legal holds, PTZ, and discover cameras. Actual media never flows through
the API, and every action re-checks its capability at the route; this file only decides what the
panel shows. Paths use the manifest's `{id}`/`{token}` placeholders.
"""
from __future__ import annotations

from typing import Any


ADMIN_ACTIONS: list[dict[str, Any]] = [
    # ── Sources ──────────────────────────────────────────────────────────────
    {"id": "stream.list_sources", "label": "List cameras", "method": "GET",
     "path": "/api/v1/sources", "capability": "platform.stream", "group": "Cameras"},
    {"id": "stream.register_source", "label": "Register a camera", "method": "POST",
     "path": "/api/v1/sources", "capability": "platform.stream", "group": "Cameras", "confirm": True,
     "params": [{"name": "name", "in": "body", "type": "string", "required": True},
                {"name": "url", "in": "body", "type": "string", "required": True,
                 "help": "rtsp://… — credentials are sealed, never shown back"}]},
    {"id": "stream.get_source", "label": "View a camera", "method": "GET",
     "path": "/api/v1/sources/{id}", "capability": "platform.stream", "group": "Cameras",
     "params": [{"name": "id", "in": "path", "type": "string", "required": True}]},
    {"id": "stream.probe_source", "label": "Probe a camera", "method": "POST",
     "path": "/api/v1/sources/{id}/probe", "capability": "platform.stream", "group": "Cameras",
     "params": [{"name": "id", "in": "path", "type": "string", "required": True}]},
    {"id": "stream.delete_source", "label": "Delete a camera", "method": "DELETE",
     "path": "/api/v1/sources/{id}", "capability": "platform.stream", "group": "Cameras",
     "danger": True, "confirm": True,
     "params": [{"name": "id", "in": "path", "type": "string", "required": True}]},
    {"id": "stream.discover", "label": "Discover cameras (ONVIF)", "method": "POST",
     "path": "/api/v1/discovery", "capability": "platform.stream", "group": "Cameras"},
    # ── Triggers ─────────────────────────────────────────────────────────────
    {"id": "stream.get_triggers", "label": "View triggers", "method": "GET",
     "path": "/api/v1/sources/{id}/triggers", "capability": "platform.stream", "group": "Triggers",
     "params": [{"name": "id", "in": "path", "type": "string", "required": True}]},
    {"id": "stream.set_triggers", "label": "Set triggers (motion → record/notify)", "method": "PUT",
     "path": "/api/v1/sources/{id}/triggers", "capability": "platform.stream", "group": "Triggers",
     "confirm": True,
     "params": [{"name": "id", "in": "path", "type": "string", "required": True}]},
    # ── Sessions ─────────────────────────────────────────────────────────────
    {"id": "stream.list_sessions", "label": "List live sessions", "method": "GET",
     "path": "/api/v1/sessions", "capability": "platform.stream", "group": "Sessions"},
    {"id": "stream.start_session", "label": "Start a live session", "method": "POST",
     "path": "/api/v1/sessions", "capability": "platform.stream", "group": "Sessions", "confirm": True,
     "params": [{"name": "source_id", "in": "body", "type": "string", "required": True},
                {"name": "mode", "in": "body", "type": "enum", "required": False,
                 "enum": ["on_demand", "viewer_driven", "continuous", "scheduled"]}]},
    {"id": "stream.get_session", "label": "View a session", "method": "GET",
     "path": "/api/v1/sessions/{id}", "capability": "platform.stream", "group": "Sessions",
     "params": [{"name": "id", "in": "path", "type": "string", "required": True}]},
    {"id": "stream.stop_session", "label": "Stop a session", "method": "POST",
     "path": "/api/v1/sessions/{id}/stop", "capability": "platform.stream", "group": "Sessions",
     "confirm": True,
     "params": [{"name": "id", "in": "path", "type": "string", "required": True}]},
    {"id": "stream.kill_session", "label": "Force-stop a session", "method": "DELETE",
     "path": "/api/v1/sessions/{id}", "capability": "platform.stream", "group": "Sessions",
     "danger": True, "confirm": True,
     "params": [{"name": "id", "in": "path", "type": "string", "required": True}]},
    # ── Recordings ───────────────────────────────────────────────────────────
    {"id": "stream.list_recordings", "label": "List recordings", "method": "GET",
     "path": "/api/v1/recordings", "capability": "platform.stream", "group": "Recordings"},
    {"id": "stream.get_recording", "label": "View a recording", "method": "GET",
     "path": "/api/v1/recordings/{id}", "capability": "platform.stream", "group": "Recordings",
     "params": [{"name": "id", "in": "path", "type": "string", "required": True}]},
    {"id": "stream.lock_recording", "label": "Lock a recording (legal hold)", "method": "POST",
     "path": "/api/v1/recordings/{id}/lock", "capability": "platform.stream", "group": "Recordings",
     "confirm": True,
     "params": [{"name": "id", "in": "path", "type": "string", "required": True}]},
    {"id": "stream.unlock_recording", "label": "Unlock a recording", "method": "POST",
     "path": "/api/v1/recordings/{id}/unlock", "capability": "platform.stream", "group": "Recordings",
     "params": [{"name": "id", "in": "path", "type": "string", "required": True}]},
    {"id": "stream.delete_recording", "label": "Delete a recording", "method": "DELETE",
     "path": "/api/v1/recordings/{id}", "capability": "platform.stream", "group": "Recordings",
     "danger": True, "confirm": True,
     "params": [{"name": "id", "in": "path", "type": "string", "required": True}]},
    # ── PTZ ──────────────────────────────────────────────────────────────────
    {"id": "stream.ptz_move", "label": "PTZ move", "method": "POST",
     "path": "/api/v1/sources/{id}/ptz/move", "capability": "platform.stream", "group": "PTZ",
     "params": [{"name": "id", "in": "path", "type": "string", "required": True},
                {"name": "pan", "in": "body", "type": "number", "required": False},
                {"name": "tilt", "in": "body", "type": "number", "required": False},
                {"name": "zoom", "in": "body", "type": "number", "required": False}]},
    {"id": "stream.ptz_stop", "label": "PTZ stop", "method": "POST",
     "path": "/api/v1/sources/{id}/ptz/stop", "capability": "platform.stream", "group": "PTZ",
     "params": [{"name": "id", "in": "path", "type": "string", "required": True}]},
    {"id": "stream.ptz_presets", "label": "PTZ presets", "method": "GET",
     "path": "/api/v1/sources/{id}/ptz/presets", "capability": "platform.stream", "group": "PTZ",
     "params": [{"name": "id", "in": "path", "type": "string", "required": True}]},
    {"id": "stream.ptz_goto", "label": "PTZ go to preset", "method": "POST",
     "path": "/api/v1/sources/{id}/ptz/goto/{token}", "capability": "platform.stream", "group": "PTZ",
     "params": [{"name": "id", "in": "path", "type": "string", "required": True},
                {"name": "token", "in": "path", "type": "string", "required": True}]},
]
