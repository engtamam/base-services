"""What backup-service must capture to restore ITSELF.

Yes — the backup orchestrator is itself a service with state worth restoring: the registry of
targets it protects, and the log of runs that proves whether backups happened. A platform that
restored every service EXCEPT the thing that knows what to back up would come back not knowing what
to back up. So backup-service is a target like any other, and declares its own collections here.
"""
from __future__ import annotations

from typing import Any

BACKUP: dict[str, Any] = {
    "object_prefix": "",
    "collections": [
        {"name": "backup_targets", "tenant_scoped": False},   # the services it protects
        {"name": "backup_runs", "tenant_scoped": False},      # the history that raises the alarm
        {"name": "backup_restores", "tenant_scoped": False},  # every restore/undo, for the record
        {"name": "backup_drills", "tenant_scoped": False},    # the DR-works evidence, every drill
        # The operator credentials (target internal tokens). Without these, restoring backup-service
        # brings back WHAT to protect but not the tokens to reach any target — every backup then 401s
        # until each secret is re-provisioned by hand (backup-012). Rows are sealed under SECRETS_KEK
        # and re-sealed under BACKUP_KEK in the archive, and carry id==key, so they round-trip safely.
        {"name": "operator_secrets", "tenant_scoped": False},
    ],
    "excluded": [],
}
