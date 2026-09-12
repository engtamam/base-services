"""The record of every backup run — what the alert reads to know a run was missed, stale or failed.

A run that nobody can see the result of is a run that cannot raise an alarm. Each run is stored with
its status and its per-target outcomes, so an operator (and the staleness check) can answer the only
question that matters: is there a recent, complete, verified backup of every service?
"""
from __future__ import annotations

from typing import Any

_COLL = "backup_runs"
_RECENT_SCAN = 10_000   # the run/restore/drill logs are bounded by retention; read the lot to sort


class RunStore:
    def __init__(self, store) -> None:
        self._s = store

    async def record(self, run: dict[str, Any]) -> None:
        await self._s.put(_COLL, run["id"], run)

    async def get(self, run_id: str) -> dict | None:
        return await self._s.get(_COLL, run_id)

    async def remove(self, run_id: str) -> bool:
        """Drop a run record — used by the retention sweep after its archives are deleted."""
        return await self._s.delete(_COLL, run_id)

    async def recent(self, limit: int) -> list[dict]:
        """The most recent runs, newest first — what the staleness alert reads."""
        # sweeper: the run log is the orchestrator's own control data, global and bounded by
        # retention, so reading it whole IS the operation, not a shortcut around an index.
        # Read the WHOLE (bounded) collection then sort — scan_all(_COLL, limit) returns the
        # key-ASCENDING first `limit`, i.e. the OLDEST runs (ids are run_<timestamp>), so "latest run"
        # (recent(1)) was actually the earliest. Sort the full set newest-first and take `limit`.
        rows = await self._s.scan_all(_COLL, _RECENT_SCAN)
        return sorted(rows, key=lambda r: r.get("started_at", ""), reverse=True)[:limit]
