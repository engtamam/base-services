"""compliance-service — admin actions.

An operator can OPEN a DSAR and place/release a legal hold, but there is deliberately no action to
force-close a DSAR: completion is DERIVED from the targets' acks, never declared, so exposing a
"mark complete" button would defeat the guarantee. The panel shows status; it cannot forge it.
Every action re-checks `platform.compliance` at the route."""
from __future__ import annotations

from typing import Any

ADMIN_ACTIONS: list[dict[str, Any]] = [
    {
        "id": "compliance.open_dsar",
        "label": "Open a DSAR (erase / export)",
        "method": "POST",
        "path": "/api/v1/compliance/dsar",
        "capability": "platform.compliance",
        "group": "DSAR",
        "danger": True,
        "confirm": True,
        "params": [
            {"name": "subject", "in": "body", "type": "string", "required": True},
            {"name": "type", "in": "body", "type": "enum", "required": True,
             "enum": ["erase", "export"]},
        ],
    },
    {
        "id": "compliance.place_legal_hold",
        "label": "Place legal hold",
        "method": "POST",
        "path": "/api/v1/compliance/legal-holds",
        "capability": "platform.compliance",
        "group": "Legal hold",
        "confirm": True,
    },
    {
        "id": "compliance.release_legal_hold",
        "label": "Release legal hold",
        "method": "POST",
        "path": "/api/v1/compliance/legal-holds/{id}/release",
        "capability": "platform.compliance",
        "group": "Legal hold",
        "danger": True,
        "confirm": True,
        "params": [{"name": "id", "in": "path", "type": "string", "required": True}],
    },
    {
        "id": "compliance.report_breach",
        "label": "Report a breach",
        "method": "POST",
        "path": "/api/v1/compliance/breaches",
        "capability": "platform.compliance",
        "group": "Breach",
        "confirm": True,
    },
    {
        "id": "compliance.notify_breach",
        "label": "Notify a breach",
        "method": "POST",
        "path": "/api/v1/compliance/breaches/{id}/notify",
        "capability": "platform.compliance",
        "group": "Breach",
        "danger": True,
        "confirm": True,
        "params": [{"name": "id", "in": "path", "type": "string", "required": True}],
    },
]
