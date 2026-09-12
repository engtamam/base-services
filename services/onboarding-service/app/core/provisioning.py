"""Provisioning — the orchestration the Saga was built for.

Signup is not one write. It is a tenant in tenant-service, a membership in identity-service, and
possibly a credit in wallet-service, and onboarding owns none of them. What it owns is the ORDER
and the obligation to put the world back when a later step fails.

The failure this exists to prevent, observed in a real codebase: provision the tenant, register the
owner, send the email. Owner registration fails. The tenant is already created, nothing un-creates
it, and recovery is a manual operator call. Retrying does not help — a retry re-runs the step that
failed, not the ones that succeeded, and if the failure is permanent (duplicate email, refused
registration) the retry loops forever while the orphan sits there.

Two properties make this safe to call repeatedly, which matters because a customer who gets a
timeout WILL press the button again:

**Idempotent forward.** Every step carries a stable key derived from the tenant, the subject and
the step name. A downstream service that has already done the work returns `exists`, and the run
continues rather than creating a second tenant.

**Resumable backward.** A compensation that fails partway leaves the still-undone steps on the
record. That list IS the queue — the same insight Phase 37 used for unacked erasure targets. A
resumed rollback re-attempts undos that already succeeded, which is why every undo treats
"already gone" as success.

What cannot be undone is RECORDED as residue rather than pretended away. An email cannot be
unsent. An operator needs to see what the world is still carrying, not a rollback that claims
everything is clean.
"""
from __future__ import annotations

import hashlib
from typing import Any

from app.core.saga import Saga, Step
from app.ports.provisioner import ProvisionRequest
from app.core.pii import subject_ref
from app.config import get_settings


class ProvisioningService:
    def __init__(self, *, provisioners: dict, store, clock, auditor=None,
                 telemetry=None) -> None:
        self._provisioners = provisioners
        self._store = store
        self._clock = clock
        self._auditor = auditor
        self._telemetry = telemetry

    async def provision(self, *, tenant: str, subject: str,
                        steps: list[dict[str, Any]]) -> dict[str, Any]:
        """Run the declared provisioning steps, compensating on failure.

        `steps` is a list of {kind, attributes}, in the order they must happen. Order is the
        caller's declaration, not an inference: a membership needs its tenant to exist first, and
        only the person declaring the flow knows that.
        """
        saga_steps: list[Step] = []
        missing = [s["kind"] for s in steps if s["kind"] not in self._provisioners]
        if missing:
            # Refused before anything is created. Discovering a missing provisioner halfway through
            # means compensating work we should never have started.
            return {"ok": False, "error": f"no provisioner configured for: {', '.join(missing)}",
                    "completed": [], "compensated": [], "residue": [], "unfinished": []}

        for spec in steps:
            saga_steps.append(self._step(tenant, subject, spec))

        run_key = self._run_key(tenant, subject)
        # SERIALIZE per (tenant, subject): two concurrent /provision runs for the same subject used to
        # interleave, and one run's compensation could tear down a resource the other had just
        # confirmed (onboarding-r2-01). An atomic advisory lock (cas insert-if-absent) admits one; a
        # second concurrent run is refused rather than corrupting the first.
        if not await self._store.cas("provisioning_locks", run_key, 0,
                                     {"id": run_key, "at": self._clock.now()}):
            return {"ok": False, "error": "provisioning_in_progress", "completed": [],
                    "compensated": [], "residue": [], "unfinished": []}
        try:
            outcome = await Saga(saga_steps, telemetry=self._telemetry).run()
            record = {
                "id": run_key,
                "tenant": tenant, "subject": subject,
                "at": self._clock.now(),
                **outcome.to_dict(),
            }
            # MERGE, don't blind-overwrite: a prior run's still-unfinished compensation / residue is a
            # work list nobody has cleared yet; a later clean run must not erase it (onboarding-r2-02).
            # Carry forward prior entries whose step this run did not itself complete/compensate.
            record = await self._merge_run_record(run_key, record)
            # Persisted whether it succeeded or not: a failed run with unfinished compensation is the
            # work list someone has to come back to, and an unrecorded failure is one nobody can find.
            await self._store.put("provisioning_runs", record["id"], record)
        finally:
            await self._store.delete("provisioning_locks", run_key)

        if not outcome.ok and self._auditor is not None:
            await self._auditor.record(
                "provisioning.compensated", tenant=tenant, resource=f"subject:{subject_ref(get_settings().PII_SALT, tenant, subject)}",
                metadata={"failed_step": outcome.failed_step,
                          "compensated": outcome.compensated,
                          "unfinished": [u["step"] for u in outcome.unfinished],
                          "residue": [r["step"] for r in outcome.residue]})
        return record

    async def _merge_run_record(self, run_key: str, record: dict[str, Any]) -> dict[str, Any]:
        """Union a prior run's still-open unfinished/residue entries into this run's record, dropping
        any whose step THIS run has since completed or compensated (onboarding-r2-02). A later clean
        run therefore never erases an earlier run's outstanding cleanup queue."""
        prior = await self._store.get("provisioning_runs", run_key)
        if not prior:
            return record
        done = set(record.get("completed", [])) | set(record.get("compensated", []))

        def _carry(field: str) -> list[dict[str, str]]:
            merged = {e.get("step"): e for e in (prior.get(field) or []) if e.get("step") not in done}
            for e in record.get(field) or []:          # this run's view wins for a step it touched
                merged[e.get("step")] = e
            return list(merged.values())

        record = dict(record)
        record["unfinished"] = _carry("unfinished")
        record["residue"] = _carry("residue")
        return record

    def _step(self, tenant: str, subject: str, spec: dict[str, Any]) -> Step:
        kind = spec["kind"]
        provisioner = self._provisioners[kind]
        request = ProvisionRequest(
            kind=kind, tenant=tenant, subject=subject,
            key=self._step_key(tenant, subject, kind),
            attributes=spec.get("attributes") or {})

        async def do() -> str:
            result = await provisioner.create(request)
            if not result.ok:
                # Raised so the Saga compensates. The port promises not to raise; this is the one
                # place a failure MUST become an exception, because that is the Saga's signal.
                raise RuntimeError(f"{kind}: {result.detail or result.status}")
            return result.reference

        async def undo(reference: str) -> None:
            result = await provisioner.undo(request, reference or "")
            if not result.ok:
                raise RuntimeError(f"undo {kind}: {result.detail}")

        return Step(name=kind, do=do,
                    undo=None if spec.get("irreversible") else undo,
                    irreversible_reason=spec.get("irreversible", ""))

    @staticmethod
    def _step_key(tenant: str, subject: str, kind: str) -> str:
        """Stable across retries and across restarts.

        Derived rather than random, so the SAME signup attempt produces the same key on the second
        press of the button — which is what lets the downstream service dedupe instead of creating
        a second tenant.
        """
        return "prv_" + hashlib.sha256(f"{tenant}|{subject}|{kind}".encode()).hexdigest()[:32]

    @staticmethod
    def _run_key(tenant: str, subject: str) -> str:
        return "run_" + hashlib.sha256(f"{tenant}|{subject}".encode()).hexdigest()[:32]

    async def resume(self, tenant: str, subject: str) -> dict[str, Any] | None:
        """Re-attempt the undos a previous compensation could not finish.

        The unfinished list is the queue. Returns None when there is nothing to resume, so a
        sweeper can call this for everyone cheaply.
        """
        record = await self._store.get("provisioning_runs", self._run_key(tenant, subject))
        if not record or not record.get("unfinished"):
            return None
        # Deliberately NOT re-running the forward steps: those succeeded, and re-running them would
        # create what we are trying to remove.
        still: list[dict[str, str]] = []
        for entry in record["unfinished"]:
            kind = entry["step"]
            provisioner = self._provisioners.get(kind)
            if provisioner is None:
                still.append(entry)
                continue
            request = ProvisionRequest(kind=kind, tenant=tenant, subject=subject,
                                       key=self._step_key(tenant, subject, kind))
            result = await provisioner.undo(request, entry.get("reference", ""))
            if not result.ok:
                still.append({**entry, "error": result.detail})
        record["unfinished"] = still
        record["resumed_at"] = self._clock.now()
        await self._store.put("provisioning_runs", record["id"], record)
        return record
