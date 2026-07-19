"""Retention enforcement — delete what we are no longer allowed to keep.

RoPA rows have carried `retention_days` since day one, and nothing ever acted on it. A declared
retention period that is never enforced is worse than none at all: the RoPA tells a regulator "we
keep this for 365 days" while the row from 2019 is still sitting in the table, so the document
becomes evidence AGAINST the platform. This closes that gap.

Scope, and why it stops here:

  Retention is AGE-scoped ("delete records older than N days"), while the erase contract is
  SUBJECT-scoped ("forget this person"). They are different questions, and only the owning service
  can answer the age one — it knows which timestamp on its rows is the retention clock, which rows
  are still referenced, and which of its own obligations override. So each service enforces its own
  retention and DECLARES the period in its RoPA row; compliance-service enforces the periods for
  the records IT owns, and the RoPA is what makes every other service's period auditable.

Two rules that are not negotiable:

  * **A legal hold beats retention, exactly as it beats erasure.** Expiry is a reason to delete;
    a hold is a reason we are not allowed to. The hold wins, and the row is reported as retained
    rather than silently skipped.
  * **Nothing is purged without a configured period.** A missing rule means "we have not decided",
    and the safe reading of an undecided retention is KEEP. Deleting on a default would destroy
    records nobody chose to destroy — and unlike over-retention, that is not reversible.
"""
from __future__ import annotations

from typing import Any

DAY_SECONDS = 86400


class RetentionEnforcer:
    def __init__(self, *, store, holds, rules: dict[str, int], clock,
                 batch: int = 500, max_per_run: int = 5000, telemetry=None) -> None:
        self._store = store
        self._holds = holds
        self._rules = rules              # collection -> days (0/absent = keep, never purge)
        self._clock = clock
        self._batch = batch
        self._max = max_per_run
        self._telemetry = telemetry

    async def run(self) -> dict[str, Any]:
        now = self._clock.now()
        purged: dict[str, int] = {}
        held: dict[str, int] = {}
        for collection, days in sorted(self._rules.items()):
            if days <= 0:
                continue                 # undecided means KEEP
            p, h = await self._purge(collection, now - days * DAY_SECONDS)
            if p:
                purged[collection] = p
            if h:
                held[collection] = h
        summary = {"purged": purged, "held_by_legal_hold": held,
                   "collections": sorted(k for k, v in self._rules.items() if v > 0)}
        self._emit("retention.run", summary)
        return summary

    async def _purge(self, collection: str, cutoff: float) -> tuple[int, int]:
        purged = held = seen = 0
        cursor = None
        # An unfiltered paginated walk. The store cannot express "age_field < cutoff" across the
        # several fields that serve as a retention clock, and this is a BACKGROUND job with a hard
        # per-run cap — never a request path, where a scan would be a latency bug.
        while seen < self._max:
            rows, next_cursor = await self._store.find(
                collection, {}, limit=self._batch, after=cursor, sort=("created_at", "asc"))
            if not rows:
                break
            before = purged
            for row in rows:
                seen += 1
                age_at = _clock_field(row)
                # No early exit on the first in-window row: rows are sorted by created_at but a
                # DSAR ages from completed_at, so an old-but-recently-closed row would stop the
                # walk and leave genuinely expired rows behind it undeleted.
                if age_at is None or age_at >= cutoff:
                    continue
                if await self._is_held(row):
                    held += 1
                    continue
                await self._store.delete(collection, row["id"])
                purged += 1

            # Deleting shifts the collection under the cursor. Advancing past a page we just
            # deleted from would skip exactly as many rows as we removed — they would survive every
            # future run too, since the same skip happens each time. So the cursor only advances
            # when the page was left intact; otherwise we re-read from the same point. This
            # terminates because every non-advancing pass removes at least one row.
            if purged > before:
                continue
            if not next_cursor:
                break
            cursor = next_cursor
        return purged, held

    async def _is_held(self, row: dict) -> bool:
        """A hold beats retention. Rows with no subject (a RoPA entry, a config row) cannot be
        held by a subject hold, so they age out normally."""
        tenant, subject = row.get("tenant"), row.get("subject")
        if not tenant or not subject:
            return False
        return bool(await self._holds.active_for(tenant, subject))

    def _emit(self, event: str, payload: dict) -> None:
        if self._telemetry is None:
            return
        try:
            self._telemetry.event(event, payload)
        except Exception:
            pass


def _clock_field(row: dict) -> float | None:
    """The retention clock for a row: when it was created, or when it reached its final state.

    A DSAR's clock starts when it COMPLETED, not when it was filed — a request that took 29 days
    should not be eligible for deletion the day after it closes.
    """
    for field in ("completed_at", "resolved_at", "at", "created_at"):
        value = row.get(field)
        if value:
            return float(value)
    return None
