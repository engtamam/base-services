"""What backup-service must capture to restore feature-flag-service, and what it may skip.

A pure declaration, read as a literal. The `safety:backup-coverage` gate checks it against the
collections the code actually writes, so a collection added later without a line here fails the build.

Flags, segments, remote config (both the head pointer and the append-only version history), and the
maintenance switches are the platform's runtime behaviour — restore them wrong and the platform comes
back with different flags than it went down with. None is keyed by a row id; each declares the key
template that rebuilds its datastore key from the row's own fields (flags/segments by "{key}", the
config head by "{key}::{env}", a config version by "{key}::{env}::{version}", maintenance by
"{module}"). These are platform-global operator config, so all are tenant_scoped=False.
"""
from __future__ import annotations

from typing import Any

BACKUP: dict[str, Any] = {
    "object_prefix": "",
    "collections": [
        {"name": "flags", "tenant_scoped": False, "key": "{key}"},
        {"name": "segments", "tenant_scoped": False, "key": "{key}"},
        {"name": "config", "tenant_scoped": False, "key": "{key}::{env}"},                 # head pointer
        {"name": "config_versions", "tenant_scoped": False, "key": "{key}::{env}::{version}"},
        {"name": "maintenance", "tenant_scoped": False, "key": "{module}"},
    ],
    "excluded": [
        {"name": "global",
         "reason": "NOT a collection — a false positive. config_service calls "
                   "`self._store.get(\"global\")` where `_store` is a MaintenanceStore (not the "
                   "datastore) and \"global\" is the default maintenance MODULE key; the real data "
                   "lives in the `maintenance` collection above, which IS backed up. Listed here so "
                   "the coverage gate's receiver heuristic, which cannot see the type, stays green"},
    ],
}
