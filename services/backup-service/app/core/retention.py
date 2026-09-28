"""Pruning old backups — bounded storage that NEVER deletes a service's only working backup.

Retention is where a naive backup system quietly destroys itself: it deletes the oldest runs to save
space and, on the day every recent run failed for one service, deletes that service's last good
backup along with them. So the one inviolable rule here is the edge-case table's: a failed backup
never deletes the last working one.

Two limits, both operator-editable (46.6): keep at least N recent runs, and optionally drop runs
older than a max age. On TOP of both, the most recent COMPLETE+VERIFIED backup of every target is
always retained, however old — it is the thing a restore would actually use, and losing it to a
tidy-up is the failure this exists to prevent.
"""
from __future__ import annotations

import datetime as _dt
from typing import Any


def _epoch(iso: str) -> float | None:
    if not iso:
        return None
    try:
        return _dt.datetime.strptime(iso, "%Y-%m-%dT%H:%M:%SZ").replace(
            tzinfo=_dt.timezone.utc).timestamp()
    except (ValueError, TypeError):
        return None


def _last_good_run_ids(ordered: list[dict]) -> set[str]:
    """The run id holding the most recent verified backup of each target — never pruned."""
    protected: set[str] = set()
    seen: set[str] = set()
    for run in ordered:                       # newest first
        for r in run.get("results", []):
            svc = r.get("service")
            if svc in seen:
                continue
            if r.get("status") == "ok" and r.get("verified"):
                protected.add(run.get("id"))
                seen.add(svc)
    return protected


class RetentionSweeper:
    def __init__(self, *, runs, object_store, iso) -> None:
        self._runs = runs
        self._objects = object_store
        self._iso = iso

    async def prune(self, *, keep_runs: int, max_age_seconds: int, scan_limit: int) -> dict[str, Any]:
        """Delete runs beyond the retention limits, keeping every target's last verified backup.

        Order matters: a run's archives are deleted first, then its record — so a crash mid-prune
        leaves an orphan archive (harmless, swept next time) rather than a run record that points at
        objects already gone.
        """
        # Enumerate the FULL run collection, not just the newest `scan_limit` (backup-005): the old
        # recent(scan_limit) sliced to 200, so any run older than the newest 200 was never seen and its
        # archives never deleted — an unbounded storage leak. all_runs() enumerates the whole
        # (retention-bounded) collection so every run past the window is actually pruned.
        all_runs = await self._runs.all_runs()               # newest first, whole collection
        protected = _last_good_run_ids(all_runs)
        now = _epoch(self._iso()) or 0.0

        pruned: list[str] = []
        kept_protected: list[str] = []
        archives_deleted = 0
        for index, run in enumerate(all_runs):
            run_id = run.get("id")
            age = now - (_epoch(run.get("started_at", "")) or now)
            too_old = index >= keep_runs or (max_age_seconds > 0 and age > max_age_seconds)
            if not too_old:
                continue
            if run_id in protected:
                kept_protected.append(run_id)      # old, but it is someone's last good backup
                continue
            # Delete this run's archives first, then the record.
            for r in run.get("results", []):
                key = r.get("archive_key")
                if key:
                    try:
                        await self._objects.delete(key)
                        archives_deleted += 1
                    except Exception:  # noqa: BLE001 — an already-gone object is fine; keep pruning
                        pass
            await self._runs.remove(run_id)
            pruned.append(run_id)

        return {"scanned": len(all_runs), "pruned": pruned, "pruned_count": len(pruned),
                "kept_protected": kept_protected, "archives_deleted": archives_deleted,
                "keep_runs": keep_runs, "max_age_seconds": max_age_seconds}
