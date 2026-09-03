"""<service> — admin actions declared for the platform admin panel (Phase 33).

The panel aggregates what every service declares HERE (it is served on /internal/manifest, so the
code is the source and the panel can never drift). Declaring nothing is fine — a service with an
empty list simply exposes no controls; keep it empty and reviewed rather than forgotten.

Each action is a PURE literal dict (the safety:admin CI gate reads it with ast.literal_eval — no
variables, no f-strings). Shape (docs/contracts/admin-actions.schema.json):

    {
        "id": "svc.do_thing",              # stable, kebab/dot, unique
        "label": "Do the thing",           # what the button says
        "method": "POST",                  # GET|POST|PUT|PATCH|DELETE (allow-listed)
        "path": "/api/v1/things/{id}/do",  # RELATIVE — no scheme/host; must be a real manifest route
        "capability": "platform.svc",      # the cap the route ENFORCES; must be advertised in the manifest
        "params": [{"name": "id", "in": "path", "type": "string", "required": True}],
        "confirm": True,                   # UX: ask before firing (auto-true when danger)
        "danger": True,                    # UX: destructive/irreversible
        "group": "Things",                 # UX: section
    }

Rules the gate enforces: the path must exist in this service's manifest `endpoints`; the capability
must be in the manifest's advertised `capabilities`; the panel is a MAP, never the AUTHORITY — every
action is re-checked at its own route on every call. Only declare OPERATOR (platform.* / *.manage /
*.read) routes; internal-token and per-user `_SELF` routes are never panel actions.
"""
from __future__ import annotations

from typing import Any


ADMIN_ACTIONS = [
    # ── Operator controls: credentials and policy, changeable after launch ────
    # A panel that can only ACT is half a panel: the reads are declared too, so an operator can
    # see which credential is loaded before replacing it. No action here returns a value — the
    # list shows a four-character hint, and safety:no-secret-reads fails the build if that
    # ever changes.
    {
        "id": "operator.list_secrets", "label": "Provider credentials", "method": "GET",
        "path": "/api/v1/backup/operator/secrets", "capability": "platform.secrets",
        "group": "Credentials",
    },
    {
        "id": "operator.set_secret", "label": "Set / rotate a provider credential", "method": "PUT",
        "path": "/api/v1/backup/operator/secrets", "capability": "platform.secrets",
        "group": "Credentials", "confirm": True,
        "params": [
            {"name": "name", "in": "body", "type": "string", "required": True,
             "help": "the setting it replaces, e.g. TAQNYAT_TOKEN or RESEND_API_KEY"},
            {"name": "value", "in": "body", "type": "string", "required": True,
             "help": "write-only: it is sealed on arrival and no route can read it back"},
            {"name": "tenant", "in": "body", "type": "string", "required": False,
             "help": "leave empty for platform-wide"},
        ],
    },
    {
        "id": "operator.clear_secret", "label": "Remove a provider credential", "method": "DELETE",
        "path": "/api/v1/backup/operator/secrets/{name}", "capability": "platform.secrets",
        "group": "Credentials", "confirm": True,
        "params": [{"name": "name", "in": "path", "type": "string", "required": True}],
    },
    {
        "id": "operator.get_config", "label": "Runtime settings", "method": "GET",
        "path": "/api/v1/backup/operator/config", "capability": "platform.config", "group": "Settings",
    },
    {
        "id": "operator.set_config", "label": "Change a runtime setting", "method": "PUT",
        "path": "/api/v1/backup/operator/config", "capability": "platform.config",
        "group": "Settings", "confirm": True,
        "params": [
            {"name": "key", "in": "body", "type": "string", "required": True,
             "help": "e.g. EMAIL_CHAIN to reorder providers, or SMS_CHAIN"},
            {"name": "value", "in": "body", "type": "string", "required": True},
            {"name": "tenant", "in": "body", "type": "string", "required": False},
        ],
    },
    {
        "id": "operator.clear_config", "label": "Reset a setting to its default", "method": "DELETE",
        "path": "/api/v1/backup/operator/config/{key}", "capability": "platform.config",
        "group": "Settings", "confirm": True,
        "params": [{"name": "key", "in": "path", "type": "string", "required": True}],
    },
    # ── Restore & disaster recovery ───────────────────────────────────────────
    # The read (plan) comes before the act (restore): an operator previews which services a backup
    # can restore, and which are blocked because they were never verified, BEFORE overwriting a
    # thing. The restore itself is danger + confirm, and echoes the run id to fire.
    {
        "id": "restore.plan", "label": "Preview a restore", "method": "POST",
        "path": "/api/v1/backup/operator/restore/plan", "capability": "platform.restore",
        "group": "Restore",
        "params": [
            {"name": "run_id", "in": "body", "type": "string", "required": True,
             "help": "the backup run to restore from (see the backup runs list)"},
            {"name": "tenant", "in": "body", "type": "string", "required": False,
             "help": "restore just one tenant's data; empty = the whole platform"},
            {"name": "services", "in": "body", "type": "array", "required": False,
             "help": "restore only these services; empty = every service in the backup"},
            {"name": "require_verified", "in": "body", "type": "boolean", "required": False,
             "help": "default true — only ever restore an archive proven restorable"},
        ],
    },
    {
        "id": "restore.run", "label": "Restore from a backup", "method": "POST",
        "path": "/api/v1/backup/operator/restore", "capability": "platform.restore",
        "group": "Restore", "confirm": True, "danger": True,
        "params": [
            {"name": "run_id", "in": "body", "type": "string", "required": True},
            {"name": "confirm", "in": "body", "type": "string", "required": True,
             "help": "repeat the run id to confirm — a restore overwrites live data"},
            {"name": "tenant", "in": "body", "type": "string", "required": False},
            {"name": "into_tenant", "in": "body", "type": "string", "required": False,
             "help": "restore-as-clone: stand the data up under a DIFFERENT tenant"},
            {"name": "services", "in": "body", "type": "array", "required": False},
            {"name": "overwrite", "in": "body", "type": "boolean", "required": False,
             "help": "default false — leave rows written after the backup alone"},
            {"name": "require_verified", "in": "body", "type": "boolean", "required": False},
            {"name": "safety_snapshot", "in": "body", "type": "boolean", "required": False,
             "help": "default true — snapshot current state first, so the restore is undoable"},
        ],
    },
    {
        "id": "restore.undo", "label": "Undo a restore", "method": "POST",
        "path": "/api/v1/backup/operator/restore/{restore_id}/undo", "capability": "platform.restore",
        "group": "Restore", "confirm": True, "danger": True,
        "params": [
            {"name": "restore_id", "in": "path", "type": "string", "required": True},
            {"name": "services", "in": "body", "type": "array", "required": False},
        ],
    },
    {
        "id": "restore.list", "label": "Restore history", "method": "GET",
        "path": "/api/v1/backup/operator/restores", "capability": "platform.restore", "group": "Restore",
    },
    {
        "id": "backup.health", "label": "Backup health", "method": "GET",
        "path": "/api/v1/backup/operator/backup/health", "capability": "platform.restore",
        "group": "Restore",
    },
    {
        "id": "backup.drills", "label": "Restore-drill history", "method": "GET",
        "path": "/api/v1/backup/operator/backup/drills", "capability": "platform.restore",
        "group": "Restore",
    },
    # ── Backup management — the full lifecycle a panel drives (platform.backup) ─
    # Protected services: see them, add/repoint one, or stop protecting one. The reads come first so
    # an operator can see what is protected before changing it.
    {
        "id": "backup.targets_list", "label": "Protected services", "method": "GET",
        "path": "/api/v1/backup/operator/backup/targets", "capability": "platform.backup",
        "group": "Backup",
    },
    {
        "id": "backup.target_register", "label": "Register / repoint a service", "method": "PUT",
        "path": "/api/v1/backup/operator/backup/targets", "capability": "platform.backup",
        "group": "Backup", "confirm": True,
        "params": [
            {"name": "service", "in": "body", "type": "string", "required": True,
             "help": "the service's name, e.g. billing-service"},
            {"name": "base_url", "in": "body", "type": "string", "required": True,
             "help": "its internal base URL, e.g. https://billing-service.internal — a wrong value "
                     "silently stops it being backed up"},
            {"name": "token_ref", "in": "body", "type": "string", "required": False,
             "help": "the NAME of the stored internal-token secret, never the token itself"},
            {"name": "enabled", "in": "body", "type": "boolean", "required": False,
             "help": "default true; false pauses backups for this service without forgetting it"},
            {"name": "note", "in": "body", "type": "string", "required": False},
        ],
    },
    {
        "id": "backup.target_remove", "label": "Stop protecting a service", "method": "DELETE",
        "path": "/api/v1/backup/operator/backup/targets/{service}", "capability": "platform.backup",
        "group": "Backup", "confirm": True, "danger": True,
        "params": [
            {"name": "service", "in": "path", "type": "string", "required": True},
            {"name": "confirm", "in": "query", "type": "string", "required": True,
             "help": "repeat the service name — removing a target means it stops being backed up"},
        ],
    },
    {
        "id": "backup.runs_list", "label": "Backup run history", "method": "GET",
        "path": "/api/v1/backup/operator/backup/runs", "capability": "platform.backup", "group": "Backup",
    },
    {
        "id": "backup.run_now", "label": "Back up now", "method": "POST",
        "path": "/api/v1/backup/operator/backup/run", "capability": "platform.backup",
        "group": "Backup", "confirm": True,
        "params": [
            {"name": "tenant", "in": "body", "type": "string", "required": False,
             "help": "back up just one tenant's data; empty = every service, whole platform"},
        ],
    },
    {
        "id": "backup.drill_now", "label": "Run a restore drill now", "method": "POST",
        "path": "/api/v1/backup/operator/backup/drill/run", "capability": "platform.backup",
        "group": "Backup", "confirm": True,
    },
    {
        "id": "backup.prune_now", "label": "Prune old backups now", "method": "POST",
        "path": "/api/v1/backup/operator/backup/prune", "capability": "platform.backup",
        "group": "Backup", "confirm": True, "danger": True,
        "params": [
            {"name": "confirm", "in": "query", "type": "string", "required": True,
             "help": "type 'prune' to confirm — this deletes old archives (never a last verified one)"},
        ],
    },
]
