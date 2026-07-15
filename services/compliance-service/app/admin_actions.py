"""compliance-service — admin actions (the complete _ADMIN + _READ operator surface).

An operator can OPEN, verify and process a DSAR, manage legal holds, register data targets / RoPA /
breaches, and READ every register. There is deliberately NO "force-close a DSAR" action: completion
is DERIVED from the targets' acks, never declared, so the panel shows DSAR status but cannot forge
it. Every action re-checks platform.compliance at the route; this file only decides what is shown.
"""
from __future__ import annotations

from typing import Any


ADMIN_ACTIONS: list[dict[str, Any]] = [
    # ── DSAR ─────────────────────────────────────────────────────────────────
    {"id": "compliance.list_dsar", "label": "List DSARs", "method": "GET",
     "path": "/api/v1/compliance/dsar", "capability": "platform.compliance", "group": "DSAR"},
    {"id": "compliance.open_dsar", "label": "Open a DSAR (erase / export)", "method": "POST",
     "path": "/api/v1/compliance/dsar", "capability": "platform.compliance", "group": "DSAR",
     "danger": True, "confirm": True,
     "params": [{"name": "subject", "in": "body", "type": "string", "required": True},
                {"name": "type", "in": "body", "type": "enum", "required": True,
                 "enum": ["erase", "export"]}]},
    {"id": "compliance.verify_dsar", "label": "Verify a DSAR", "method": "POST",
     "path": "/api/v1/compliance/dsar/{dsar_id}/verify", "capability": "platform.compliance", "group": "DSAR",
     "confirm": True,
     "params": [{"name": "dsar_id", "in": "path", "type": "string", "required": True}]},
    {"id": "compliance.process_dsar", "label": "Process a DSAR (fan out to targets)", "method": "POST",
     "path": "/api/v1/compliance/dsar/{dsar_id}/process", "capability": "platform.compliance", "group": "DSAR",
     "danger": True, "confirm": True,
     "params": [{"name": "dsar_id", "in": "path", "type": "string", "required": True}]},
    # ── Consent ──────────────────────────────────────────────────────────────
    {"id": "compliance.consent_history", "label": "View consent history", "method": "GET",
     "path": "/api/v1/compliance/consent/history", "capability": "platform.compliance", "group": "Consent",
     "params": [{"name": "subject", "in": "query", "type": "string", "required": True}]},
    {"id": "compliance.grant_consent", "label": "Record consent grant", "method": "POST",
     "path": "/api/v1/compliance/consent/grant", "capability": "platform.compliance", "group": "Consent"},
    {"id": "compliance.withdraw_consent", "label": "Record consent withdrawal", "method": "POST",
     "path": "/api/v1/compliance/consent/withdraw", "capability": "platform.compliance", "group": "Consent",
     "confirm": True},
    # ── Legal hold ───────────────────────────────────────────────────────────
    {"id": "compliance.list_legal_holds", "label": "List legal holds", "method": "GET",
     "path": "/api/v1/compliance/legal-holds", "capability": "platform.compliance", "group": "Legal hold"},
    {"id": "compliance.place_legal_hold", "label": "Place legal hold", "method": "POST",
     "path": "/api/v1/compliance/legal-holds", "capability": "platform.compliance", "group": "Legal hold",
     "confirm": True},
    {"id": "compliance.release_legal_hold", "label": "Release legal hold", "method": "POST",
     "path": "/api/v1/compliance/legal-holds/{id}/release", "capability": "platform.compliance", "group": "Legal hold",
     "danger": True, "confirm": True,
     "params": [{"name": "id", "in": "path", "type": "string", "required": True}]},
    # ── Data map / RoPA ──────────────────────────────────────────────────────
    {"id": "compliance.list_data_targets", "label": "List data targets", "method": "GET",
     "path": "/api/v1/compliance/data-targets", "capability": "platform.compliance", "group": "Data map"},
    {"id": "compliance.register_data_target", "label": "Register data target", "method": "POST",
     "path": "/api/v1/compliance/data-targets", "capability": "platform.compliance", "group": "Data map"},
    {"id": "compliance.list_ropa", "label": "List RoPA", "method": "GET",
     "path": "/api/v1/compliance/ropa", "capability": "platform.compliance", "group": "Data map"},
    {"id": "compliance.create_ropa", "label": "Record processing activity (RoPA)", "method": "POST",
     "path": "/api/v1/compliance/ropa", "capability": "platform.compliance", "group": "Data map"},
    # ── Breach ───────────────────────────────────────────────────────────────
    {"id": "compliance.list_breaches", "label": "List breaches", "method": "GET",
     "path": "/api/v1/compliance/breaches", "capability": "platform.compliance", "group": "Breach"},
    {"id": "compliance.register_breach", "label": "Register a breach", "method": "POST",
     "path": "/api/v1/compliance/breaches", "capability": "platform.compliance", "group": "Breach", "confirm": True},
    {"id": "compliance.notify_breach", "label": "Notify a breach", "method": "POST",
     "path": "/api/v1/compliance/breaches/{id}/notify", "capability": "platform.compliance", "group": "Breach",
     "danger": True, "confirm": True,
     "params": [{"name": "id", "in": "path", "type": "string", "required": True}]},
]
