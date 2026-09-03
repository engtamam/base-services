"""file-service — admin actions: the operator controls, and nothing else.

The self-service file routes — upload / list / download / delete, and the large direct-to-bucket
flow — operate ONLY on the caller's own tenant and owner, taken from the token, under the ordinary
files.upload / files.read grants. Mutation stays self-service: there is no cross-tenant delete or
upload from the panel, and the scan-drain and session sweeper are internal-token routes.

The OPERATOR surface is read-only and platform-gated: the credentials and policy an operator manages
(platform.secrets / platform.config), and — under platform.files — browsing and inspecting ANY
tenant's files for support. The platform-wide reader already existed in the file-access check; this
adds the browse it lacked and advertises the capability. Every cross-tenant look is audited.
"""
from __future__ import annotations

from typing import Any


ADMIN_ACTIONS: list[dict[str, Any]] = [
    # ── Operator controls: credentials and policy, changeable after launch ────
    # A panel that can only ACT is half a panel: the reads are declared too, so an operator can
    # see which credential is loaded before replacing it. No action here returns a value — the
    # list shows a four-character hint, and safety:no-secret-reads fails the build if that
    # ever changes.
    {
        "id": "operator.list_secrets", "label": "Service credentials", "method": "GET",
        "path": "/api/v1/files/operator/secrets", "capability": "platform.secrets",
        "group": "Credentials",
    },
    {
        "id": "operator.set_secret", "label": "Set / rotate a service credential", "method": "PUT",
        "path": "/api/v1/files/operator/secrets", "capability": "platform.secrets",
        "group": "Credentials", "confirm": True,
        "params": [
            {"name": "name", "in": "body", "type": "string", "required": True,
             "help": "the setting it replaces, e.g. SIGNED_URL_SECRET"},
            {"name": "value", "in": "body", "type": "string", "required": True,
             "help": "write-only: it is sealed on arrival and no route can read it back"},
            {"name": "tenant", "in": "body", "type": "string", "required": False,
             "help": "leave empty for platform-wide"},
        ],
    },
    {
        "id": "operator.clear_secret", "label": "Remove a service credential", "method": "DELETE",
        "path": "/api/v1/files/operator/secrets/{name}", "capability": "platform.secrets",
        "group": "Credentials", "confirm": True,
        "params": [{"name": "name", "in": "path", "type": "string", "required": True}],
    },
    {
        "id": "operator.get_config", "label": "Runtime settings", "method": "GET",
        "path": "/api/v1/files/operator/config", "capability": "platform.config", "group": "Settings",
    },
    {
        "id": "operator.set_config", "label": "Change a runtime setting", "method": "PUT",
        "path": "/api/v1/files/operator/config", "capability": "platform.config",
        "group": "Settings", "confirm": True,
        "params": [
            {"name": "key", "in": "body", "type": "string", "required": True,
             "help": "e.g. SIGNED_URL_TTL_SECONDS or MAX_UPLOAD_BYTES"},
            {"name": "value", "in": "body", "type": "string", "required": True},
            {"name": "tenant", "in": "body", "type": "string", "required": False},
        ],
    },
    {
        "id": "operator.clear_config", "label": "Reset a setting to its default", "method": "DELETE",
        "path": "/api/v1/files/operator/config/{key}", "capability": "platform.config",
        "group": "Settings", "confirm": True,
        "params": [{"name": "key", "in": "path", "type": "string", "required": True}],
    },
    # ── File management: browse and inspect ANY tenant's files (platform.files) ─
    # The platform-wide reader existed in code but could only OPEN a known file id; there was no way
    # to browse to one, and platform.files was not even advertised. Both fixed. Read-only — deletion
    # and upload stay self-service — and every cross-tenant look is audited.
    {
        "id": "files.browse", "label": "Browse a tenant's files", "method": "GET",
        "path": "/api/v1/files/admin/list", "capability": "platform.files", "group": "Files",
        "params": [
            {"name": "tenant", "in": "query", "type": "string", "required": True,
             "help": "the tenant whose files to browse"},
            {"name": "limit", "in": "query", "type": "integer", "required": False,
             "help": "page size (clamped by the service)"},
            {"name": "after", "in": "query", "type": "string", "required": False,
             "help": "keyset cursor from the previous page"},
        ],
    },
    {
        "id": "files.inspect", "label": "Inspect a file's metadata", "method": "GET",
        "path": "/api/v1/files/admin/{file_id}/metadata", "capability": "platform.files",
        "group": "Files",
        "params": [{"name": "file_id", "in": "path", "type": "string", "required": True,
                    "help": "the file id, across any tenant"}],
    },
]
