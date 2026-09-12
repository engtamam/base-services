"""The record of every restore and undo — because a restore is the action most in need of a record.

A restore overwrites live data. Who ran it, from which backup, into which tenant, what each target
reported, and where the pre-restore snapshot lives (so it can be undone) — all of it is kept here.
An incident review, a regulator, or the operator the morning after will reconstruct what happened
from these rows; nowhere else holds it.
"""
from __future__ import annotations

from typing import Any

_COLL = "backup_restores"


class RestoreStore:
    def __init__(self, store) -> None:
        self._s = store

    async def record(self, restore: dict[str, Any]) -> None:
        await self._s.put(_COLL, restore["id"], restore)

    async def get(self, restore_id: str) -> dict | None:
        return await self._s.get(_COLL, restore_id)

    async def recent(self, limit: int) -> list[dict]:
        """The most recent restores, newest first — the restore history an operator reviews."""
        # sweeper: the restore log is this service's own control data, global and bounded by
        # retention, so reading it whole IS the operation, not a shortcut around an index.
        # scan_all returns the key-ASCENDING first `limit` (oldest); read the whole bounded
        # collection and sort newest-first so "recent" is genuinely the newest.
        rows = await self._s.scan_all(_COLL, 10_000)
        return sorted(rows, key=lambda r: r.get("at", ""), reverse=True)[:limit]
