"""file-service — end-user (self-service) actions: a user's OWN files.

file-service is ALL self-service (its admin_actions is deliberately empty — there is no operator
surface), so the user app is where its actions live: upload / list / download / share (signed URL) /
delete files, and the large direct-to-bucket flow (initiate / complete / status / cancel). Every
route is scoped to the caller's tenant and owner from the token — a user only ever touches their own
files. Real capabilities gate them: files.upload for writes, files.read for reads (both advertised).
The app is a map — each route re-checks its capability and re-scopes to the caller on every call.
"""
from __future__ import annotations

from typing import Any


USER_ACTIONS: list[dict[str, Any]] = [
    # ── everyday files ────────────────────────────────────────────────────────
    {"id": "files.list", "label": "My files", "method": "GET",
     "path": "/api/v1/files", "capability": "files.read", "group": "Files"},
    {"id": "files.upload", "label": "Upload a file", "method": "POST",
     "path": "/api/v1/files", "capability": "files.upload", "group": "Files"},
    {"id": "files.download", "label": "Download a file", "method": "GET",
     "path": "/api/v1/files/{id}", "capability": "files.read", "group": "Files",
     "params": [{"name": "id", "in": "path", "type": "string", "required": True}]},
    {"id": "files.metadata", "label": "File details", "method": "GET",
     "path": "/api/v1/files/{id}/metadata", "capability": "files.read", "group": "Files",
     "params": [{"name": "id", "in": "path", "type": "string", "required": True}]},
    {"id": "files.share", "label": "Get a share link", "method": "POST",
     "path": "/api/v1/files/{id}/signed-url", "capability": "files.read", "group": "Files",
     "params": [{"name": "id", "in": "path", "type": "string", "required": True}]},
    {"id": "files.delete", "label": "Delete a file", "method": "DELETE",
     "path": "/api/v1/files/{id}", "capability": "files.upload", "group": "Files",
     "danger": True, "confirm": True,
     "params": [{"name": "id", "in": "path", "type": "string", "required": True}]},
    # ── large, direct-to-bucket upload ────────────────────────────────────────
    {"id": "files.large_initiate", "label": "Start a large upload", "method": "POST",
     "path": "/api/v1/files/uploads", "capability": "files.upload", "group": "Large uploads",
     "params": [{"name": "sha256", "in": "body", "type": "string", "required": True},
                {"name": "size", "in": "body", "type": "integer", "required": True}]},
    {"id": "files.large_complete", "label": "Finish a large upload", "method": "POST",
     "path": "/api/v1/files/uploads/{id}/complete", "capability": "files.upload",
     "group": "Large uploads",
     "params": [{"name": "id", "in": "path", "type": "string", "required": True}]},
    {"id": "files.large_status", "label": "Large-upload status", "method": "GET",
     "path": "/api/v1/files/uploads/{id}", "capability": "files.read", "group": "Large uploads",
     "params": [{"name": "id", "in": "path", "type": "string", "required": True}]},
    {"id": "files.large_cancel", "label": "Cancel a large upload", "method": "DELETE",
     "path": "/api/v1/files/uploads/{id}", "capability": "files.upload", "group": "Large uploads",
     "confirm": True,
     "params": [{"name": "id", "in": "path", "type": "string", "required": True}]},
]
