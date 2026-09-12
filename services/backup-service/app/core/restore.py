"""Restoring a backup — the one genuinely dangerous thing this service does.

Backup is automatic and safe; restore overwrites live data and is not. Everything here exists to make
a restore something an operator can do deliberately and undo if it was a mistake, never something
that happens by accident or destroys the evidence of what it changed.

THE FOUR RULES A RESTORE OBEYS:

  1. RESTORE ONLY WHAT WAS PROVEN. An archive that was never verified (46.4) is not restored — you do
     not overwrite live data from something you never confirmed can be restored. This is the whole
     point of the verification pass made load-bearing: `require_verified` is on by default and turning
     it off is an explicit, audited choice.

  2. SNAPSHOT BEFORE YOU OVERWRITE. Before a target is restored, its CURRENT state is captured and
     stored as a pre-restore snapshot, so the restore is itself undoable. If that snapshot cannot be
     taken, the target is NOT restored (unless the operator explicitly waives it) — a restore you
     cannot roll back is exactly the mistake that turns a bad restore into a catastrophe.

  3. NEVER ABORT THE WHOLE RESTORE ON ONE TARGET. Each target is restored in isolation and its
     outcome recorded; one service failing does not deny the others their restore, and the failure is
     surfaced rather than swallowed.

  4. WRITE A RECORD OF EVERY RESTORE. Who restored what, from which backup, into which tenant, and
     what each target reported — because a restore is the action a regulator, an incident review, or
     the operator themselves will most need to reconstruct.

backup-service still never opens a target's database. It reads the sealed archive it stored and hands
it back to the target's own /internal/backup/import; what a row means and how it is keyed stays the
target's knowledge.
"""
from __future__ import annotations

import secrets as _secrets

import asyncio
import json
from dataclasses import dataclass
from typing import Any

from app.core.internal_http import TargetUnreachable, post_json

# Per-target restore outcomes.
RESTORED = "restored"
SKIPPED = "skipped"        # not eligible: not in the backup, not verified, or filtered out
FAILED = "failed"          # attempted and could not complete (unreachable, snapshot failed, refused)

def _status(results: list["TargetRestore"]) -> str:
    """The restore's overall status. A MIX of success and failure is 'partial', not 'failed' — an
    operator must be able to tell "some came back, some did not" from "nothing came back at all",
    because the next action is completely different."""
    if not results:
        return "empty"
    restored = sum(1 for r in results if r.status == RESTORED)
    if restored == len(results):
        return "restored"
    if restored:
        return "partial"
    if any(r.status == FAILED for r in results):
        return "failed"
    return "skipped"      # nothing eligible (e.g. every target was unverified)


@dataclass
class TargetRestore:
    service: str
    status: str
    inserted: int = 0
    overwritten: int = 0
    skipped: int = 0
    remapped: int = 0
    snapshot_key: str = ""     # the pre-restore snapshot, for undo
    reason: str = ""


def _eligible(run: dict, service: str) -> tuple[bool, str]:
    """Was `service` captured AND verified in this run? Returns (ok, why-not)."""
    for r in run.get("results", []):
        if r.get("service") == service:
            if r.get("status") != "ok":
                return False, f"target was {r.get('status')} in this backup, not ok"
            if not r.get("verified"):
                return False, "target's archive in this backup was never verified"
            if not r.get("archive_key"):
                return False, "target has no stored archive in this backup"
            return True, ""
    return False, "target was not part of this backup"


class RestoreOrchestrator:
    def __init__(self, *, targets, object_store, runs, restores, resolve_token, iso,
                 cfg: dict[str, Any]) -> None:
        self._targets = targets
        self._objects = object_store
        self._runs = runs
        self._restores = restores
        self._resolve_token = resolve_token
        self._iso = iso
        self._cfg = cfg

    # ── preview, writes nothing ───────────────────────────────────────────────
    async def plan(self, run_id: str, *, tenant: str = "", services: list[str] | None = None,
                   require_verified: bool = True) -> dict[str, Any]:
        """What a restore of `run_id` WOULD do, without writing a thing.

        The preview an operator confirms against: which targets are eligible, which are not and why,
        and how many rows each holds. A restore should never be the first time you learn one of the
        services you meant to restore was never verified.
        """
        run = await self._runs.get(run_id)
        if not run:
            return {"run_id": run_id, "found": False, "targets": []}
        want = set(services or [])
        preview = []
        for r in run.get("results", []):
            svc = r.get("service")
            if want and svc not in want:
                continue
            ok, why = _eligible(run, svc)
            eligible = ok or not require_verified
            preview.append({
                "service": svc, "eligible": eligible, "verified": bool(r.get("verified")),
                "status_in_backup": r.get("status"), "row_counts": r.get("row_counts", {}),
                "blocked_reason": "" if ok else why,
            })
        return {"run_id": run_id, "found": True, "taken_at": run.get("started_at"),
                "backup_status": run.get("status"), "tenant": tenant,
                "require_verified": require_verified, "targets": preview}

    # ── execute ───────────────────────────────────────────────────────────────
    async def restore(self, run_id: str, *, actor: str = "", tenant: str = "",
                      into_tenant: str = "", services: list[str] | None = None,
                      overwrite: bool = False, require_verified: bool = True,
                      safety_snapshot: bool = True) -> dict[str, Any]:
        """Restore a backup run across its targets. Returns the recorded restore."""
        restore_id = f"restore_{self._iso().replace(':', '').replace('-', '')}_{_secrets.token_hex(3)}"
        run = await self._runs.get(run_id)
        if not run:
            raise RestoreError(f"backup run {run_id!r} does not exist")

        want = set(services or [])
        results: list[TargetRestore] = []
        for r in run.get("results", []):
            svc = r.get("service")
            if want and svc not in want:
                continue
            results.append(await self._restore_one(
                run, restore_id, svc, r, tenant=tenant, into_tenant=into_tenant,
                overwrite=overwrite, require_verified=require_verified,
                safety_snapshot=safety_snapshot))

        record = {
            "id": restore_id,
            "at": self._iso(),
            "actor": actor,
            "from_run": run_id,
            "tenant": tenant,
            "into_tenant": into_tenant,
            "overwrite": overwrite,
            "require_verified": require_verified,
            "status": _status(results),
            "restored": [t.service for t in results if t.status == RESTORED],
            "skipped": [t.service for t in results if t.status == SKIPPED],
            "failed": [t.service for t in results if t.status == FAILED],
            "results": [t.__dict__ for t in results],
        }
        await self._restores.record(record)
        return record

    async def _restore_one(self, run: dict, restore_id: str, service: str, result: dict, *,
                           tenant: str, into_tenant: str, overwrite: bool,
                           require_verified: bool, safety_snapshot: bool) -> TargetRestore:
        """Restore one target. Never raises — a failure is a recorded outcome, not a stop."""
        ok, why = _eligible(run, service)
        if not ok and require_verified:
            return TargetRestore(service, SKIPPED, reason=why)

        target = await self._targets.get(service)
        if target is None or not target.base_url:
            return TargetRestore(service, FAILED, reason="target is no longer registered")
        base = target.base_url.rstrip("/")
        allowed = self._cfg["egress_allowed_hosts"]
        timeout = self._cfg["target_timeout_seconds"]
        try:
            token = await self._resolve_token(target.token_ref)
        except Exception as exc:  # noqa: BLE001
            return TargetRestore(service, FAILED, reason=f"could not resolve token: {type(exc).__name__}")

        # Rule 2: snapshot the CURRENT state before overwriting it, so this restore is undoable.
        snapshot_key = ""
        if safety_snapshot:
            try:
                # WHOLE-SERVICE snapshot (tenant=""), not the caller's `tenant`: the import below
                # overwrites the target's full state (import_archive applies the whole stored archive
                # with no tenant filter), so the undo point MUST capture the same scope or undo() can
                # only restore a subset — leaving the rest of the overwrite unrecoverable. The `tenant`
                # arg does NOT scope the restore (import has no tenant filter); scoping it is a
                # separate feature (see the audit note), and a partial snapshot was the unsafe part.
                snapshot_key = await self._snapshot(base, service, restore_id, token, "",
                                                    allowed, timeout)
            except _SnapshotFailed as exc:
                return TargetRestore(service, FAILED,
                                     reason=f"pre-restore snapshot failed, refused to overwrite "
                                            f"without an undo point: {exc}")

        # Read the archive we stored, and hand it back to the target's own importer.
        archive_key = result.get("archive_key", "")
        try:
            archive = json.loads((await self._objects.get(archive_key)).decode("utf-8"))
        except Exception as exc:  # noqa: BLE001
            return TargetRestore(service, FAILED, snapshot_key=snapshot_key,
                                 reason=f"stored archive could not be read: {type(exc).__name__}")
        try:
            status, body = await asyncio.to_thread(   # BLOCKING urlopen off the loop (see v0.18.3)
                post_json,
                f"{base}/internal/backup/import",
                {"archive": archive, "overwrite": overwrite, "into_tenant": into_tenant},
                token, allowed_hosts=allowed, timeout_seconds=timeout)
        except TargetUnreachable as exc:
            return TargetRestore(service, FAILED, snapshot_key=snapshot_key,
                                 reason=f"target unreachable during restore: {exc}")
        if not (200 <= status < 300):
            return TargetRestore(service, FAILED, snapshot_key=snapshot_key,
                                 reason=f"import returned {status}: {body.get('detail', '')}")
        return TargetRestore(
            service, RESTORED, snapshot_key=snapshot_key,
            inserted=int(body.get("inserted", 0)), overwritten=int(body.get("overwritten", 0)),
            skipped=int(body.get("skipped", 0)), remapped=int(body.get("remapped", 0)))

    async def _snapshot(self, base: str, service: str, restore_id: str, token: str, tenant: str,
                        allowed, timeout) -> str:
        """Capture the target's current state and store it as a pre-restore undo point."""
        try:
            status, archive = await asyncio.to_thread(   # BLOCKING urlopen off the loop (see v0.18.3)
                post_json, f"{base}/internal/backup/export", {"tenant": tenant},
                token, allowed_hosts=allowed, timeout_seconds=timeout)
        except TargetUnreachable as exc:
            raise _SnapshotFailed(str(exc)) from exc
        if not (200 <= status < 300) or archive.get("format") != "base-backup/v1":
            raise _SnapshotFailed(f"export returned {status}")
        key = f"{restore_id}/pre/{service}.json"
        await self._objects.put(key, json.dumps(archive).encode("utf-8"), "application/json")
        return key

    # ── undo: put back the pre-restore snapshot ───────────────────────────────
    async def undo(self, restore_id: str, *, actor: str = "",
                   services: list[str] | None = None) -> dict[str, Any]:
        """Roll a restore back, by restoring each target's pre-restore snapshot over the top.

        This is the safety net rule 2 exists to provide: a restore that turned out wrong is not a
        one-way door. Overwrite is forced — the point is to put back exactly what was there before.
        """
        prior = await self._restores.get(restore_id)
        if not prior:
            raise RestoreError(f"restore {restore_id!r} does not exist")
        undo_id = f"undo_{self._iso().replace(':', '').replace('-', '')}_{_secrets.token_hex(3)}"
        want = set(services or [])
        results: list[TargetRestore] = []
        for r in prior.get("results", []):
            svc = r.get("service")
            snap = r.get("snapshot_key")
            if want and svc not in want:
                continue
            if not snap:
                results.append(TargetRestore(svc, SKIPPED, reason="no pre-restore snapshot to undo"))
                continue
            results.append(await self._undo_one(svc, snap, undo_id))

        record = {
            "id": undo_id, "at": self._iso(), "actor": actor, "undoes": restore_id,
            "status": _status(results),
            "restored": [t.service for t in results if t.status == RESTORED],
            "failed": [t.service for t in results if t.status == FAILED],
            "results": [t.__dict__ for t in results],
        }
        await self._restores.record(record)
        return record

    async def _undo_one(self, service: str, snapshot_key: str, undo_id: str) -> TargetRestore:
        target = await self._targets.get(service)
        if target is None or not target.base_url:
            return TargetRestore(service, FAILED, reason="target is no longer registered")
        base = target.base_url.rstrip("/")
        allowed = self._cfg["egress_allowed_hosts"]
        timeout = self._cfg["target_timeout_seconds"]
        try:
            token = await self._resolve_token(target.token_ref)
            archive = json.loads((await self._objects.get(snapshot_key)).decode("utf-8"))
        except Exception as exc:  # noqa: BLE001
            return TargetRestore(service, FAILED, reason=f"could not read snapshot: {type(exc).__name__}")
        try:
            status, body = await asyncio.to_thread(   # BLOCKING urlopen off the loop (see v0.18.3)
                post_json, f"{base}/internal/backup/import",
                {"archive": archive, "overwrite": True},
                token, allowed_hosts=allowed, timeout_seconds=timeout)
        except TargetUnreachable as exc:
            return TargetRestore(service, FAILED, reason=f"target unreachable during undo: {exc}")
        if not (200 <= status < 300):
            return TargetRestore(service, FAILED, reason=f"undo import returned {status}")
        return TargetRestore(service, RESTORED, snapshot_key=snapshot_key,
                             inserted=int(body.get("inserted", 0)),
                             overwritten=int(body.get("overwritten", 0)))


class RestoreError(ValueError):
    """A restore that cannot proceed — an unknown run, or a caller error worth a 4xx."""


class _SnapshotFailed(RuntimeError):
    """The pre-restore snapshot could not be taken; without it a restore has no undo point."""
