"""Provisioning with compensation — no orphans when a later step fails.

The concrete gap this closes, observed in a real codebase: signup provisions a tenant, then
registers the owner, then sends a verification email. If the owner registration fails, the tenant
is already created and nothing un-creates it. There is no rollback anywhere in the path, and
recovery is a manual operator call. The audit doc in that repo acknowledges the area.

It is the same defect family as the rest of this work — each step correct, the seam between them
unhandled — and it is worth stating why "just retry" is not the answer. A retry re-runs the step
that failed; it does not undo the steps that succeeded. If the failure is permanent (a duplicate
email, a refused registration), retrying loops forever while the orphan sits there.

So provisioning is modelled as a list of REVERSIBLE actions, each carrying its own undo, and a
failure walks the completed ones backwards.

Four rules earn their place:

**Compensation runs in reverse.** The tenant was created before the owner, so the owner is removed
before the tenant. Forward order would try to delete a tenant that still has members.

**Every undo is idempotent.** Compensation itself can fail halfway and be resumed, and a
resumed run will re-attempt undos that already succeeded.

**What cannot be undone is RECORDED, not pretended away.** A sent email cannot be unsent; credit
already spent cannot be clawed back. These become `residue` on the record with a reason — an
operator can see exactly what the world is still carrying, instead of a rollback that claims
everything is clean.

**A failed compensation is loud and resumable.** The record keeps the actions still to undo, so the
unfinished work IS the queue — the same insight Phase 37 used for unacked erasure targets.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable

PENDING, DONE, COMPENSATED, FAILED = "pending", "done", "compensated", "failed"


@dataclass
class Step:
    """One provisioning action and how to take it back.

    `undo` may be None for something genuinely irreversible — that is not a hole, it is the honest
    shape. It surfaces as residue rather than being silently skipped.
    """
    name: str
    do: Callable[[], Awaitable[Any]]
    undo: Callable[[Any], Awaitable[None]] | None = None
    irreversible_reason: str = ""
    state: str = PENDING
    result: Any = None
    error: str = ""


@dataclass
class SagaOutcome:
    ok: bool
    completed: list[str] = field(default_factory=list)
    compensated: list[str] = field(default_factory=list)
    # What the world is still carrying that we could not take back. Named, with reasons.
    residue: list[dict[str, str]] = field(default_factory=list)
    # Undos that failed. The unfinished work IS the queue — a resumed run retries exactly these.
    unfinished: list[dict[str, str]] = field(default_factory=list)
    failed_step: str = ""
    error: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {"ok": self.ok, "completed": self.completed, "compensated": self.compensated,
                "residue": self.residue, "unfinished": self.unfinished,
                "failed_step": self.failed_step, "error": self.error}


class Saga:
    """Run steps forward; on failure, walk the successful ones backwards."""

    def __init__(self, steps: list[Step], *, telemetry=None) -> None:
        self._steps = steps
        self._telemetry = telemetry

    async def run(self) -> SagaOutcome:
        done: list[Step] = []

        for step in self._steps:
            try:
                step.result = await step.do()
                step.state = DONE
                done.append(step)
            except Exception as exc:
                step.state = FAILED
                step.error = str(exc)
                outcome = await self._compensate(done)
                outcome.failed_step = step.name
                outcome.error = str(exc)
                outcome.ok = False
                self._emit("onboarding.provision.failed", outcome.to_dict())
                return outcome

        return SagaOutcome(ok=True, completed=[s.name for s in done])

    async def _compensate(self, done: list[Step]) -> SagaOutcome:
        """Undo what succeeded, newest first.

        Reverse order matters: the tenant was created before the owner, so the owner must go first
        — deleting a tenant that still has members would fail, and we would then be compensating
        the compensation.
        """
        outcome = SagaOutcome(ok=False, completed=[s.name for s in done])

        for step in reversed(done):
            if step.undo is None:
                # Genuinely irreversible. Recorded rather than skipped: an operator needs to know
                # the email went out and the credit was issued, even though the signup failed.
                outcome.residue.append({
                    "step": step.name,
                    "why": step.irreversible_reason or "no undo defined for this action",
                })
                continue
            try:
                await step.undo(step.result)
                step.state = COMPENSATED
                outcome.compensated.append(step.name)
            except Exception as exc:
                # Loud, and resumable. This is the list a retry works from.
                outcome.unfinished.append({"step": step.name, "error": str(exc)})

        return outcome

    def _emit(self, event: str, payload: dict) -> None:
        if self._telemetry is None:
            return
        try:
            self._telemetry.event(event, payload)
        except Exception:
            pass          # observability must never break the rollback
