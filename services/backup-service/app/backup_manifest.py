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
    ],
    "excluded": [],
}
