"""What backup-service must capture to restore stream-service, and what it may skip.

A pure declaration, read as a literal. The `safety:backup-coverage` gate checks it against the
collections the code actually writes, so a collection added later without a line here fails the build.

All five collections are id-keyed (datastore key == row id; triggers stores id = its source id). These
are the METADATA rows — sources, sessions, recordings, exports, triggers. The recorded MEDIA itself
lives in the configured sink / object store and is backed up by that store's own lifecycle, not
sealed into this archive; object_prefix is empty because this service writes its records to the
datastore, and the blob bytes are not the platform's to duplicate here.
"""
from __future__ import annotations

from typing import Any

BACKUP: dict[str, Any] = {
    "object_prefix": "",
    "collections": [
        {"name": "sources", "tenant_scoped": True},
        {"name": "sessions", "tenant_scoped": True},
        {"name": "recordings", "tenant_scoped": True},
        {"name": "exports", "tenant_scoped": True},
        {"name": "triggers", "tenant_scoped": True},   # id = source id
    ],
    "excluded": [],
}
