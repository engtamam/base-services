"""Capturing a backup run across every registered target.

backup-service never opens a target's database. It calls the target's own export endpoint, receives
a sealed, checksummed archive, verifies the checksum before storing it, and records what happened.
What a collection means and how its rows are keyed stays the target's knowledge; this is pure
orchestration.

THE ONE RULE THAT MAKES A RUN HONEST: a target that could not be captured is recorded as MISSED,
never dropped. "We backed up" while one service was unreachable is the precise lie this whole phase
exists to kill — a run's status is the WORST of its targets', so a single miss makes the run
incomplete rather than letting nineteen successes report green over a twentieth service's silent
gap.

A RUN DOES NOT ABORT ON ONE BAD TARGET. Each is captured in isolation, so a service that is down
does not deny a backup to the other nineteen. Its miss is retried on the next run, and surfaced now.

THE ARCHIVE IS VERIFIED BEFORE IT IS STORED. The target's checksum is recomputed here; a corrupt
archive is refused and its target marked failed, so the backup store never holds something a restore
would reject. Storing garbage and discovering it at restore time is the failure mode that makes
"we have backups" false.
"""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from typing import Any

from app.core.internal_http import TargetUnreachable, post_json

# Per-target outcomes, worst last so `max` over their ranks gives the run's status.
OK = "ok"
MISSED = "missed"        # unreachable this run — retryable, never silently skipped
FAILED = "failed"        # answered, but with something a restore could not use (bad checksum)

_RANK = {OK: 0, MISSED: 1, FAILED: 2}
_RUN_STATUS = {0: "complete", 1: "incomplete", 2: "failed"}

_ARCHIVE_FORMAT = "base-backup/v1"


@dataclass
class TargetResult:
    service: str
    status: str
    archive_key: str = ""
    row_counts: dict[str, int] = field(default_factory=dict)
    error: str = ""


def _checksum(sealed: str) -> str:
    return "sha256:" + hashlib.sha256(sealed.encode("utf-8")).hexdigest()


class Orchestrator:
    def __init__(self, *, targets, object_store, runs, resolve_token, clock, iso,
                 cfg: dict[str, Any]) -> None:
        self._targets = targets
        self._objects = object_store
        self._runs = runs                      # RunStore: records each run for the alert to read
        self._resolve_token = resolve_token    # (token_ref) -> token, via the credential store
        self._clock = clock
        self._iso = iso
        self._cfg = cfg

    async def _capture_one(self, target, run_id: str, tenant: str) -> TargetResult:
        """Export one target and store its archive. Never raises: a failure is a result, not a stop."""
        base = target.base_url.rstrip("/")
        allowed = self._cfg["egress_allowed_hosts"]
        timeout = self._cfg["target_timeout_seconds"]
        try:
            token = await self._resolve_token(target.token_ref)
            status, archive = post_json(f"{base}/internal/backup/export", {"tenant": tenant},
                                        token, allowed_hosts=allowed, timeout_seconds=timeout)
        except TargetUnreachable as exc:
            return TargetResult(target.service, MISSED, error=str(exc))
        if status == 503:
            # The target has no backup key configured — it refused rather than exporting in the
            # clear. That is correct on its side and a miss on ours: nothing to store, try later.
            return TargetResult(target.service, MISSED, error="target has no backup key (503)")
        if not (200 <= status < 300):
            return TargetResult(target.service, MISSED, error=f"export returned {status}")

        # Verify BEFORE storing: the store must never hold an archive a restore would reject.
        if archive.get("format") != _ARCHIVE_FORMAT:
            return TargetResult(target.service, FAILED, error="unrecognised archive format")
        sealed, checksum = archive.get("sealed"), archive.get("checksum")
        if not sealed or not checksum or _checksum(sealed) != checksum:
            return TargetResult(target.service, FAILED,
                                error="archive checksum failed — corrupt or truncated export")

        key = f"{run_id}/{target.service}.json"
        await self._objects.put(key, json.dumps(archive).encode("utf-8"), "application/json")
        return TargetResult(target.service, OK, archive_key=key,
                            row_counts=archive.get("row_counts", {}))

    async def run(self, *, tenant: str = "") -> dict[str, Any]:
        """Capture every enabled target. The run's status is the worst target's."""
        run_id = f"run_{self._iso().replace(':', '').replace('-', '')}"
        targets = await self._targets.enabled(self._cfg["target_limit"])
        results = [await self._capture_one(t, run_id, tenant) for t in targets]

        worst = max((_RANK[r.status] for r in results), default=0)
        run = {
            "id": run_id,
            "started_at": self._iso(),
            "tenant": tenant,
            "status": _RUN_STATUS[worst],
            "targets": len(results),
            "ok": sum(1 for r in results if r.status == OK),
            "missed": [r.service for r in results if r.status == MISSED],
            "failed": [r.service for r in results if r.status == FAILED],
            "results": [r.__dict__ for r in results],
        }
        await self._runs.record(run)
        return run
