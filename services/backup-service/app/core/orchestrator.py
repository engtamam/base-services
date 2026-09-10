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

AND THE STORED ARCHIVE IS PROVEN TO RESTORE. A checksum proves the bytes are intact, not that they
RESTORE. So after storing, the archive is read BACK from the store (catching any corruption the
store introduced at rest) and handed to the target's own /internal/backup/verify, which test-
restores it into a throwaway namespace and compares. backup-service cannot do this itself — it
cannot unseal another service's archive and does not know its schema — so the verdict comes from
the one party that can. An archive that will not restore is UNVERIFIED or FAILED, never OK: this
phase's whole belief is that an unverified backup counts as no backup at all.
"""
from __future__ import annotations

import asyncio
import hashlib
import json
from dataclasses import dataclass, field
from typing import Any

from app.core.internal_http import TargetUnreachable, post_json

# Per-target outcomes, worst last so `max` over their ranks gives the run's status.
OK = "ok"
MISSED = "missed"        # unreachable this run — retryable, never silently skipped
UNVERIFIED = "unverified"  # stored, but the verify test-restore could not be COMPLETED (retry next run)
FAILED = "failed"        # answered, but with something a restore could not use (bad checksum, or it
                         # restored WRONG) — we KNOW this archive is not usable

_RANK = {OK: 0, MISSED: 1, UNVERIFIED: 2, FAILED: 3}
_RUN_STATUS = {0: "complete", 1: "incomplete", 2: "unverified", 3: "failed"}

_ARCHIVE_FORMAT = "base-backup/v1"


@dataclass
class TargetResult:
    service: str
    status: str
    archive_key: str = ""
    row_counts: dict[str, int] = field(default_factory=dict)
    verified: bool = False        # the archive was test-restored and compared equal
    checked: int = 0              # rows the target actually restored-and-compared
    # The distinct tenants this target's archive covered (approach a): reported by the target's
    # export in the clear, so a whole-service run can answer "was tenant X in run Y" per service —
    # and, combined with `verified`, "was X's data at this service test-restored in run Y".
    tenants_covered: list[str] = field(default_factory=list)
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
            status, archive = await asyncio.to_thread(   # BLOCKING urlopen off the loop (see v0.18.3)
                post_json, f"{base}/internal/backup/export", {"tenant": tenant},
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

        # Coverage from the (integrity-checked) archive. A list, so an old target that does not yet
        # report it simply covers nothing here — never a crash, just no per-tenant answer for it.
        tcov = [str(t) for t in (archive.get("tenants_covered") or [])]
        key = f"{run_id}/{target.service}.json"
        try:
            await self._objects.put(key, json.dumps(archive).encode("utf-8"), "application/json")
        except Exception as exc:  # noqa: BLE001 — storage full/unreachable: this ONE target fails
            # The run fails loudly for this target, but does NOT abort — the other targets still get
            # their backup, and nothing existing is deleted, so the previous good backup is retained.
            # (Storing was the last write; a failure here means simply nothing new landed for it.)
            return TargetResult(target.service, FAILED, row_counts=archive.get("row_counts", {}),
                                tenants_covered=tcov,
                                error=f"could not store archive (storage full or unreachable?): "
                                      f"{type(exc).__name__}")

        # Prove the STORED archive restores. Read it back from the store (not the in-memory copy),
        # so corruption the store introduced at rest is caught, then let the target test-restore it.
        return await self._verify_one(target, key, archive.get("row_counts", {}), tcov)

    async def _verify_one(self, target, key: str, row_counts: dict[str, int],
                          tenants_covered: list[str]) -> TargetResult:
        """Ask the target to test-restore the stored archive. Never raises: a failure is a verdict."""
        base = target.base_url.rstrip("/")
        allowed = self._cfg["egress_allowed_hosts"]
        timeout = self._cfg["target_timeout_seconds"]
        try:
            stored = json.loads((await self._objects.get(key)).decode("utf-8"))
        except Exception as exc:  # noqa: BLE001 — the archive we just wrote will not read back
            return TargetResult(target.service, FAILED, archive_key=key, row_counts=row_counts,
                                tenants_covered=tenants_covered,
                                error=f"stored archive could not be read back for verification: "
                                      f"{type(exc).__name__}")
        try:
            token = await self._resolve_token(target.token_ref)
            status, body = await asyncio.to_thread(   # BLOCKING urlopen off the loop (see v0.18.3)
                post_json, f"{base}/internal/backup/verify", {"archive": stored},
                token, allowed_hosts=allowed, timeout_seconds=timeout)
        except TargetUnreachable as exc:
            # The archive is stored, but we could not PROVE it restores this run. Not OK, not lost:
            # UNVERIFIED, retried next run rather than reported as a good backup.
            return TargetResult(target.service, UNVERIFIED, archive_key=key, row_counts=row_counts,
                                tenants_covered=tenants_covered,
                                error=f"verify unreachable: {exc}")
        if status == 400:
            # The target refused the archive as unrestorable — bad checksum after storage, or a key
            # that no longer opens it. We KNOW it is bad.
            return TargetResult(target.service, FAILED, archive_key=key, row_counts=row_counts,
                                tenants_covered=tenants_covered,
                                error=f"verify rejected the archive: {body.get('detail', '400')}")
        if not (200 <= status < 300):
            return TargetResult(target.service, UNVERIFIED, archive_key=key, row_counts=row_counts,
                                tenants_covered=tenants_covered,
                                error=f"verify returned {status}")
        if not body.get("verified"):
            # It restored, but not to an identical copy — the archive is not a faithful backup.
            miss = body.get("mismatches") or []
            return TargetResult(target.service, FAILED, archive_key=key, row_counts=row_counts,
                                tenants_covered=tenants_covered,
                                checked=int(body.get("checked", 0)),
                                error="verify found the archive restores incorrectly"
                                      + (f": {miss[0]}" if miss else ""))
        return TargetResult(target.service, OK, archive_key=key, row_counts=row_counts,
                            tenants_covered=tenants_covered,
                            verified=True, checked=int(body.get("checked", 0)))

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
            # ok AND verified — the only rows an operator may actually rely on for a restore.
            "verified": sum(1 for r in results if r.verified),
            "missed": [r.service for r in results if r.status == MISSED],
            "unverified": [r.service for r in results if r.status == UNVERIFIED],
            "failed": [r.service for r in results if r.status == FAILED],
            "results": [r.__dict__ for r in results],
        }
        await self._runs.record(run)
        return run
