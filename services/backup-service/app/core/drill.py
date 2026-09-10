"""The restore drill — proving, on a schedule, that every service can ACTUALLY be restored now.

This is the capstone the whole phase is built toward: DR that fails almost always fails because
nobody tried a restore until they needed one. Verification at capture (46.4) proves each archive was
restorable the moment it was taken; health (46.6) reads what the records SAY. Neither re-checks the
stored bytes weeks later, when the object store may have rotted them, a format may have drifted, or a
KEK may have moved. The drill does exactly that, without ever touching live data.

For every registered target it finds the most recent VERIFIED backup — the one a real restore would
actually use — reads it back FROM THE STORE, and asks the target to test-restore it into a throwaway
namespace and compare (the same safe /internal/backup/verify path as 46.4). A target whose best
backup no longer restores is a DR failure discovered by a drill on a Tuesday, not by a customer
during an outage. The result is recorded and, if the drill fails, alerted.

It is READ-ONLY against live data: the test-restore happens in the target's throwaway namespace and
is torn down there. The drill writes only its own result row.
"""
from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass
from typing import Any

from app.core.internal_http import TargetUnreachable, post_json

PASSED = "passed"
FAILED = "failed"       # the best backup no longer restores, or none exists to drill


def _status(results: list["DrillResult"]) -> str:
    if not results:
        return "empty"
    passed = sum(1 for r in results if r.status == PASSED)
    if passed == len(results):
        return "passed"
    if passed:
        return "partial"
    return "failed"


@dataclass
class DrillResult:
    service: str
    status: str
    from_run: str = ""
    checked: int = 0
    error: str = ""


def _latest_verified(ordered_runs: list[dict], service: str) -> tuple[str, str]:
    """(run_id, archive_key) of the most recent VERIFIED backup of `service`, or ("","")."""
    for run in ordered_runs:                       # newest first
        for r in run.get("results", []):
            if (r.get("service") == service and r.get("status") == "ok"
                    and r.get("verified") and r.get("archive_key")):
                return run.get("id", ""), r.get("archive_key", "")
    return "", ""


class RestoreDrill:
    def __init__(self, *, targets, object_store, runs, drills, resolve_token, iso,
                 cfg: dict[str, Any]) -> None:
        self._targets = targets
        self._objects = object_store
        self._runs = runs
        self._drills = drills
        self._resolve_token = resolve_token
        self._iso = iso
        self._cfg = cfg

    async def run(self, *, services: list[str] | None = None) -> dict[str, Any]:
        """Drill every registered target's best backup. Records and returns the result."""
        drill_id = f"drill_{self._iso().replace(':', '').replace('-', '')}"
        targets = await self._targets.enabled(self._cfg["target_limit"])
        ordered = sorted(await self._runs.recent(self._cfg["run_history"]),
                         key=lambda r: r.get("started_at", ""), reverse=True)
        want = set(services or [])

        results: list[DrillResult] = []
        for t in targets:
            if want and t.service not in want:
                continue
            results.append(await self._drill_one(t, ordered))

        record = {
            "id": drill_id, "at": self._iso(), "status": _status(results),
            "targets": len(results),
            "passed": [r.service for r in results if r.status == PASSED],
            "failed": [r.service for r in results if r.status == FAILED],
            "results": [r.__dict__ for r in results],
        }
        await self._drills.record(record)
        return record

    async def _drill_one(self, target, ordered_runs: list[dict]) -> DrillResult:
        """Re-verify one target's best backup against the target itself. Never raises."""
        run_id, archive_key = _latest_verified(ordered_runs, target.service)
        if not archive_key:
            return DrillResult(target.service, FAILED,
                               error="no verified backup exists to drill — this service is NOT "
                                     "known-restorable")
        try:
            stored = json.loads((await self._objects.get(archive_key)).decode("utf-8"))
        except Exception as exc:  # noqa: BLE001 — the stored archive is gone or unreadable AT REST
            return DrillResult(target.service, FAILED, from_run=run_id,
                               error=f"stored backup could not be read at rest: {type(exc).__name__}")

        base = target.base_url.rstrip("/")
        allowed = self._cfg["egress_allowed_hosts"]
        timeout = self._cfg["target_timeout_seconds"]
        try:
            token = await self._resolve_token(target.token_ref)
            status, body = await asyncio.to_thread(   # BLOCKING urlopen off the loop (see v0.18.3)
                post_json, f"{base}/internal/backup/verify", {"archive": stored},
                token, allowed_hosts=allowed, timeout_seconds=timeout)
        except TargetUnreachable as exc:
            return DrillResult(target.service, FAILED, from_run=run_id,
                               error=f"target unreachable during drill: {exc}")
        if not (200 <= status < 300) or not body.get("verified"):
            miss = (body.get("mismatches") or [""])[0] if isinstance(body, dict) else ""
            return DrillResult(target.service, FAILED, from_run=run_id,
                               error=f"best backup NO LONGER restores (status {status})"
                                     + (f": {miss}" if miss else ""))
        return DrillResult(target.service, PASSED, from_run=run_id,
                           checked=int(body.get("checked", 0)))
