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
     "path": "/api/v1/compliance/consent/grant", "capability": "platform.compliance", "group": "Consent",
     "params": [
         {"name": "subject", "in": "body", "type": "string", "required": True},
         {"name": "purpose", "in": "body", "type": "string", "required": True,
          "help": "consent is per-purpose: a grant for one purpose says nothing about another"},
         {"name": "basis", "in": "body", "type": "string", "required": False,
          "help": "self | guardian — who gave it"},
         {"name": "consented_by", "in": "body", "type": "string", "required": False},
         {"name": "relation", "in": "body", "type": "string", "required": False},
         {"name": "method", "in": "body", "type": "string", "required": False},
         {"name": "notice_version", "in": "body", "type": "string", "required": False,
          "help": "the privacy notice the subject actually saw"},
         {"name": "expires_at", "in": "body", "type": "number", "required": False},
         {"name": "subject_born_at", "in": "body", "type": "number", "required": False},
     ]},
    {"id": "compliance.withdraw_consent", "label": "Record consent withdrawal", "method": "POST",
     "path": "/api/v1/compliance/consent/withdraw", "capability": "platform.compliance", "group": "Consent",
     "confirm": True,
     "params": [
         {"name": "subject", "in": "body", "type": "string", "required": True},
         {"name": "purpose", "in": "body", "type": "string", "required": True,
          "help": "withdrawal is per-purpose too — the other purposes stay granted"},
     ]},
    # ── Legal hold ───────────────────────────────────────────────────────────
    {"id": "compliance.list_legal_holds", "label": "List legal holds", "method": "GET",
     "path": "/api/v1/compliance/legal-holds", "capability": "platform.compliance", "group": "Legal hold"},
    {"id": "compliance.place_legal_hold", "label": "Place legal hold", "method": "POST",
     "path": "/api/v1/compliance/legal-holds", "capability": "platform.compliance", "group": "Legal hold",
     "confirm": True,
     "params": [
         {"name": "subject", "in": "body", "type": "string", "required": True},
         {"name": "reason", "in": "body", "type": "string", "required": True,
          "help": "a hold blocks erasure, so the reason is the record of why the law overrode a DSAR"},
     ]},
    {"id": "compliance.release_legal_hold", "label": "Release legal hold", "method": "POST",
     "path": "/api/v1/compliance/legal-holds/{id}/release", "capability": "platform.compliance", "group": "Legal hold",
     "danger": True, "confirm": True,
     "params": [{"name": "id", "in": "path", "type": "string", "required": True}]},
    # ── Data map / RoPA ──────────────────────────────────────────────────────
    {"id": "compliance.list_data_targets", "label": "List data targets", "method": "GET",
     "path": "/api/v1/compliance/data-targets", "capability": "platform.compliance", "group": "Data map"},
    {"id": "compliance.register_data_target", "label": "Register data target", "method": "POST",
     "path": "/api/v1/compliance/data-targets", "capability": "platform.compliance", "group": "Data map",
     "params": [
         {"name": "service", "in": "body", "type": "string", "required": True,
          "help": "the service that holds the data and must ack a DSAR fan-out"},
         {"name": "category", "in": "body", "type": "string", "required": True},
         {"name": "description", "in": "body", "type": "string", "required": False},
     ]},
    {"id": "compliance.unreachable_data_targets", "label": "Unreachable data targets", "method": "GET",
     "path": "/api/v1/compliance/data-targets/unreachable", "capability": "platform.compliance",
     "group": "Data map"},
    {"id": "compliance.delete_data_target", "label": "Remove a data target", "method": "DELETE",
     "path": "/api/v1/compliance/data-targets/{id}", "capability": "platform.compliance", "group": "Data map",
     "danger": True, "confirm": True,
     "params": [{"name": "id", "in": "path", "type": "string", "required": True}]},
    {"id": "compliance.list_ropa", "label": "List RoPA", "method": "GET",
     "path": "/api/v1/compliance/ropa", "capability": "platform.compliance", "group": "Data map"},
    {"id": "compliance.create_ropa", "label": "Record processing activity (RoPA)", "method": "POST",
     "path": "/api/v1/compliance/ropa", "capability": "platform.compliance", "group": "Data map",
     "params": [
         {"name": "name", "in": "body", "type": "string", "required": True},
         {"name": "purpose", "in": "body", "type": "string", "required": False},
         {"name": "lawful_basis", "in": "body", "type": "string", "required": False,
          "help": "defaults to consent"},
         {"name": "data_categories", "in": "body", "type": "array", "required": False},
         {"name": "recipients", "in": "body", "type": "array", "required": False},
         {"name": "retention_days", "in": "body", "type": "integer", "required": False},
         {"name": "residency", "in": "body", "type": "string", "required": False},
         {"name": "cross_border", "in": "body", "type": "boolean", "required": False},
     ]},
    {"id": "compliance.delete_ropa", "label": "Remove a processing activity", "method": "DELETE",
     "path": "/api/v1/compliance/ropa/{id}", "capability": "platform.compliance", "group": "Data map",
     "danger": True, "confirm": True,
     "params": [{"name": "id", "in": "path", "type": "string", "required": True}]},
    # ── Breach ───────────────────────────────────────────────────────────────
    {"id": "compliance.list_breaches", "label": "List breaches", "method": "GET",
     "path": "/api/v1/compliance/breaches", "capability": "platform.compliance", "group": "Breach"},
    {"id": "compliance.register_breach", "label": "Register a breach", "method": "POST",
     "path": "/api/v1/compliance/breaches", "capability": "platform.compliance", "group": "Breach", "confirm": True,
     "params": [
         {"name": "description", "in": "body", "type": "string", "required": True},
         {"name": "severity", "in": "body", "type": "string", "required": False,
          "help": "defaults to medium — it drives the notification deadline"},
         {"name": "affected_subjects", "in": "body", "type": "integer", "required": False},
         {"name": "data_categories", "in": "body", "type": "array", "required": False},
         {"name": "discovered_at", "in": "body", "type": "number", "required": False,
          "help": "epoch seconds; the clock for the regulator deadline starts here"},
     ]},
    {"id": "compliance.notify_breach", "label": "Notify a breach", "method": "POST",
     "path": "/api/v1/compliance/breaches/{id}/notify", "capability": "platform.compliance", "group": "Breach",
     "danger": True, "confirm": True,
     "params": [{"name": "id", "in": "path", "type": "string", "required": True}]},

    # ── Operator controls: credentials and policy, changeable after launch ────
    # A panel that can only ACT is half a panel: the reads are declared too, so an operator can
    # see which credential is loaded before replacing it. No action here returns a value — the
    # list shows a four-character hint, and safety:no-secret-reads fails the build if that
    # ever changes.
    {
        "id": "operator.list_secrets", "label": "Service credentials", "method": "GET",
        "path": "/api/v1/compliance/operator/secrets", "capability": "platform.secrets",
        "group": "Credentials",
    },
    {
        "id": "operator.set_secret", "label": "Set / rotate a credential", "method": "PUT",
        "path": "/api/v1/compliance/operator/secrets", "capability": "platform.secrets",
        "group": "Credentials", "confirm": True,
        "params": [
            {"name": "name", "in": "body", "type": "string", "required": True,
             "help": "the setting it replaces, e.g. JOB_SERVICE_TOKEN or a target's erase token"},
            {"name": "value", "in": "body", "type": "string", "required": True,
             "help": "write-only: it is sealed on arrival and no route can read it back"},
            {"name": "tenant", "in": "body", "type": "string", "required": False,
             "help": "leave empty for platform-wide"},
        ],
    },
    {
        "id": "operator.clear_secret", "label": "Remove a credential", "method": "DELETE",
        "path": "/api/v1/compliance/operator/secrets/{name}", "capability": "platform.secrets",
        "group": "Credentials", "confirm": True,
        "params": [{"name": "name", "in": "path", "type": "string", "required": True}],
    },
    {
        "id": "operator.get_config", "label": "Runtime settings", "method": "GET",
        "path": "/api/v1/compliance/operator/config", "capability": "platform.config", "group": "Settings",
    },
    {
        "id": "operator.set_config", "label": "Change a runtime setting", "method": "PUT",
        "path": "/api/v1/compliance/operator/config", "capability": "platform.config",
        "group": "Settings", "confirm": True,
        "params": [
            {"name": "key", "in": "body", "type": "string", "required": True,
             "help": "e.g. RETENTION_RULES, or DSAR_SLA_DAYS"},
            {"name": "value", "in": "body", "type": "string", "required": True},
            {"name": "tenant", "in": "body", "type": "string", "required": False},
        ],
    },
    {
        "id": "operator.clear_config", "label": "Reset a setting to its default", "method": "DELETE",
        "path": "/api/v1/compliance/operator/config/{key}", "capability": "platform.config",
        "group": "Settings", "confirm": True,
        "params": [{"name": "key", "in": "path", "type": "string", "required": True}],
    },
    {
        # The read side of the legal-pages surface: the console's markdown editor lists then opens a
        # page. The routes exist (GET /legal, GET /legal/{doc_id}, gated by _VIEW) but were never
        # declared, so the panel had no way to reach them — draft/publish with nothing to open first.
        "id": "compliance.legal_list", "label": "List legal pages", "method": "GET",
        "path": "/api/v1/compliance/legal",
        "capability": "platform.compliance", "group": "Legal pages",
    },
    {
        "id": "compliance.legal_view", "label": "View a legal page", "method": "GET",
        "path": "/api/v1/compliance/legal/{doc_id}",
        "capability": "platform.compliance", "group": "Legal pages",
        "params": [
            {"name": "doc_id", "in": "path", "type": "string", "required": True,
             "help": "slug: privacy-policy, terms-of-service, …"},
            {"name": "locale", "in": "query", "type": "string", "required": False},
            {"name": "version", "in": "query", "type": "string", "required": False,
             "help": "a specific version; omit for the current published one"},
        ],
    },
    {
        "id": "compliance.legal_draft", "label": "Draft a legal page version", "method": "PUT",
        "path": "/api/v1/compliance/legal/{doc_id}/versions/{version}",
        "capability": "platform.compliance", "group": "Legal pages",
        "params": [
            {"name": "doc_id", "in": "path", "type": "string", "required": True,
             "help": "slug: privacy-policy, terms-of-service, …"},
            {"name": "version", "in": "path", "type": "string", "required": True,
             "help": "your version string, e.g. 1.0 or 2026-08-10"},
            {"name": "content", "in": "body", "type": "object", "required": True,
             "help": "locale -> {title, sections:[{heading,body}]}; a published version cannot be edited"},
            {"name": "requires_acceptance", "in": "body", "type": "boolean", "required": False},
        ],
    },
    {
        "id": "compliance.legal_publish", "label": "Publish a legal page version", "method": "POST",
        "path": "/api/v1/compliance/legal/{doc_id}/versions/{version}/publish",
        "capability": "platform.compliance", "group": "Legal pages", "confirm": True,
        "params": [
            {"name": "doc_id", "in": "path", "type": "string", "required": True},
            {"name": "version", "in": "path", "type": "string", "required": True},
        ],
    },
    {
        "id": "compliance.legal_history", "label": "Legal page version history", "method": "GET",
        "path": "/api/v1/compliance/legal/{doc_id}/history",
        "capability": "platform.compliance", "group": "Legal pages",
        "params": [{"name": "doc_id", "in": "path", "type": "string", "required": True}],
    },
    {
        "id": "compliance.legal_templates", "label": "PDPL template pack", "method": "GET",
        "path": "/api/v1/compliance/legal/templates",
        "capability": "platform.compliance", "group": "Legal pages",
    },
    {
        "id": "compliance.legal_instantiate",
        "label": "Fill a PDPL template into a legal page", "method": "POST",
        "path": "/api/v1/compliance/legal/templates/{template_id}/instantiate",
        "capability": "platform.compliance", "group": "Legal pages", "confirm": True,
        "params": [
            {"name": "template_id", "in": "path", "type": "string", "required": True,
             "help": "e.g. doc01_privacy_policy, doc12_cookies, doc13_processing_agreement"},
            {"name": "values", "in": "body", "type": "object", "required": True,
             "help": "placeholder -> value (COMPANY_NAME, DPO_EMAIL, …); all declared ones required"},
            {"name": "version", "in": "body", "type": "string", "required": True},
            {"name": "requires_acceptance", "in": "body", "type": "boolean", "required": False},
            {"name": "publish", "in": "body", "type": "boolean", "required": False,
             "help": "false = draft only; true = publish immediately"},
        ],
    },
]
