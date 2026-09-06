"""PrivacySweeper — walk every open DSAR and re-drive whatever has not acked.

This is the worker that was missing: ten erasure endpoints existed and nothing ever called them,
so a DSAR was a record of an intention rather than an effect.

It is deliberately the thinnest possible thing on top of the dispatcher, because all the durable
state already lives in the DSAR. That gives three properties for free:

  * **Resumable.** Killed mid-sweep, the next run picks up exactly where it stopped — the unacked
    targets are still unacked.
  * **Idempotent.** Two replicas (or a cron overlapping its own previous run) re-drive the same
    request; already-acked targets are skipped and backoff suppresses duplicate dials.
  * **Bounded.** Pages through the open set with a per-run cap, so a backlog of ten thousand
    requests degrades into "several runs" instead of one run that never finishes.

One target failing must never stop the sweep. A single unreachable service would otherwise freeze
erasure for every other subject on the platform — the blast radius of one bad target has to stay at
that target.
"""
from __future__ import annotations

from typing import Any

from app.core.models import SCHEDULED


class PrivacySweeper:
    def __init__(self, *, dsars, dispatcher, page_size: int = 100,
                 max_per_run: int = 1000, telemetry=None, promote=None, clock=None) -> None:
        self._dsars = dsars
        self._dispatcher = dispatcher
        self._page = page_size
        self._max = max_per_run
        self._telemetry = telemetry
        # `promote` = DsarService.process — used to turn a due, cooling-off erasure (status SCHEDULED,
        # dispatch_at reached) into an actual fan-out. It re-checks the legal hold, so a hold placed
        # during the grace window still refuses. None (or no clock) → grace is off and the sweep only
        # re-drives already-dispatched targets, exactly as before.
        self._promote = promote
        self._clock = clock

    async def run(self) -> dict[str, Any]:
        seen = completed = errored = 0
        stuck: list[dict] = []
        cursor = None

        while seen < self._max:
            batch, cursor = await self._dsars.open_dsars(
                limit=min(self._page, self._max - seen), after=cursor)
            if not batch:
                break
            for dsar in batch:
                seen += 1
                # A cooling-off erasure whose window has elapsed: promote it to an actual dispatch
                # (process() re-checks the legal hold, then builds targets + fans out). Before the
                # window, or with grace off, it is left untouched. Everything else re-drives normally.
                if (self._promote is not None and self._clock is not None
                        and getattr(dsar, "status", "") == SCHEDULED):
                    if getattr(dsar, "dispatch_at", 0.0) and self._clock.now() >= dsar.dispatch_at:
                        try:
                            await self._promote(dsar.id)
                        except Exception as exc:
                            errored += 1
                            self._emit("privacy.sweep.error", {"dsar": dsar.id, "error": str(exc)})
                    continue     # not-yet-due scheduled erasures wait; nothing to drive yet
                try:
                    result = await self._dispatcher.drive(dsar)
                except Exception as exc:
                    # One bad target must not freeze erasure for every other subject.
                    errored += 1
                    self._emit("privacy.sweep.error", {"dsar": dsar.id, "error": str(exc)})
                    continue
                if result.get("complete"):
                    completed += 1
                stuck.extend(result.get("stuck") or [])
            if not cursor:
                break

        summary = {"swept": seen, "completed": completed, "errored": errored,
                   "stuck": stuck, "truncated": seen >= self._max}
        self._emit("privacy.sweep.done", summary)
        return summary

    def _emit(self, event: str, payload: dict) -> None:
        if self._telemetry is None:
            return
        try:
            self._telemetry.event(event, payload)
        except Exception:
            pass
