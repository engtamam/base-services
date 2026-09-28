"""The record of every backup run — what the alert reads to know a run was missed, stale or failed.

A run that nobody can see the result of is a run that cannot raise an alarm. Each run is stored with
its status and its per-target outcomes, so an operator (and the staleness check) can answer the only
question that matters: is there a recent, complete, verified backup of every service?
"""
from __future__ import annotations

from typing import Any

_COLL = "backup_runs"
_KIND = "backup_run"    # a constant discriminator so recent() is an indexed find(), not a scan+sort
_RECENT_SCAN = 10_000   # the FULL-collection sweep bound (retention) — never a request path


class RunStore:
    def __init__(self, store) -> None:
        self._s = store

    async def record(self, run: dict[str, Any]) -> None:
        # Stamp a constant kind so recent() can be an ordered, LIMIT-bounded find() (backup-003)
        # instead of scanning + sorting the whole collection on every hot-path call.
        run = {**run, "kind": _KIND}
        await self._s.put(_COLL, run["id"], run)

    async def get(self, run_id: str) -> dict | None:
        return await self._s.get(_COLL, run_id)

    async def remove(self, run_id: str) -> bool:
        """Drop a run record — used by the retention sweep after its archives are deleted."""
        return await self._s.delete(_COLL, run_id)

    async def recent(self, limit: int) -> list[dict]:
        """The most recent runs, newest first — the hot-path read (staleness alert, tenant coverage).

        A LIMIT-bounded ordered find() on a constant kind (backup-003): answering recent(1) reads one
        row, not 10,000. Filtered on `kind` (never an empty where), so it is an indexed read, not a
        scan. Runs written by an older build (no `kind`) are invisible here until the next run writes a
        kinded row — a brief window that only affects reads of already-old runs; retention (all_runs)
        still sees and protects them."""
        rows, _ = await self._s.find(_COLL, {"kind": _KIND}, limit=max(1, limit),
                                     sort=("started_at", "desc"))
        return rows

    async def all_runs(self) -> list[dict]:
        """EVERY run, newest first — the retention sweeper's full view (backup-005). Uses scan_all so
        it enumerates the WHOLE (retention-bounded) collection, including legacy rows without `kind`,
        so no run past the retention window is ever missed and left with orphaned archives forever."""
        # sweeper: retention's full-collection pass over the tiny, retention-bounded run log — not a
        # request path; the whole log IS the operation (see recent() for the hot-path indexed read).
        rows = await self._s.scan_all(_COLL, _RECENT_SCAN)
        return sorted(rows, key=lambda r: r.get("started_at", ""), reverse=True)
