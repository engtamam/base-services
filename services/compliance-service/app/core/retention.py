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

from app.core.pii import is_subject_ref, subject_ref

DAY_SECONDS = 86400


class RetentionEnforcer:
    def __init__(self, *, store, holds, rules: dict[str, int], clock,
                 max_per_run: int = 5000, telemetry=None, pii_salt: str = "") -> None:
        self._store = store
        self._holds = holds
        self._rules = rules              # collection -> days (0/absent = keep, never purge)
        self._clock = clock
        self._max = max_per_run
        self._telemetry = telemetry
        self._salt = pii_salt
        self._held_refs: dict[str, set[str]] = {}   # per-run cache: tenant -> {subject_ref of held}

    async def run(self) -> dict[str, Any]:
        self._held_refs = {}
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
        # sweeper: retention's job IS to visit every row in the collection and decide whether it has
        # outlived its declared period, so reading all of them is the operation rather than a
        # forgotten query. No filter can express it: the store speaks equality only, and the
        # retention clock is a different field per collection (completed_at for a DSAR, created_at
        # for a RoPA row). scan_all() rather than find({}) because that is what this actually is —
        # disguising a scan as a filtered read would hide it from the next reader and from this
        # gate. Runs on a nightly CronJob, never on a request path, and capped per run.
        rows = await self._store.scan_all(collection, limit=self._max)

        purged = held = 0
        for row in rows:
            age_at = _clock_field(row, collection)
            if age_at is None or age_at >= cutoff:
                continue
            if await self._is_held(row):
                held += 1                    # a hold beats retention, exactly as it beats erasure
                continue
            await self._store.delete(collection, row["id"])
            purged += 1
        return purged, held

    async def _is_held(self, row: dict) -> bool:
        """A hold beats retention. Rows with no subject (a RoPA entry, a config row) cannot be
        held by a subject hold, so they age out normally.

        compliance-012: an ERASURE-survivor proof row carries the subject's PSEUDONYM (subject_ref),
        not the raw subject a hold is keyed by — active_for(tenant, '<sub_…>') would match zero holds
        and the evidence would age out despite a hold on the underlying person. For a pseudonymised
        row, intersect its subject_ref against the subject_refs of the tenant's active holds instead."""
        tenant, subject = row.get("tenant"), row.get("subject")
        if not tenant or not subject:
            return False
        if is_subject_ref(subject):
            return subject in await self._held_refs_for(tenant)
        return bool(await self._holds.active_for(tenant, subject))

    async def _held_refs_for(self, tenant: str) -> set[str]:
        if tenant in self._held_refs:
            return self._held_refs[tenant]
        refs: set[str] = set()
        if self._salt:
            holds, _ = await self._holds.active_in_tenant(tenant, self._max)
            refs = {subject_ref(self._salt, tenant, h.subject) for h in holds if h.subject}
        self._held_refs[tenant] = refs
        return refs

    def _emit(self, event: str, payload: dict) -> None:
        if self._telemetry is None:
            return
        try:
            self._telemetry.event(event, payload)
        except Exception:
            pass


def _clock_field(row: dict, collection: str = "") -> float | None:
    """The retention clock for a row: when it reached its final state, or when it was created.

    A DSAR's clock starts when it COMPLETED, not when it was filed — a request that took 29 days
    should not be eligible for deletion the day after it closes. Crucially, an OPEN (never-terminal)
    DSAR must NEVER age out (compliance-001): the old fallback to created_at let a request stuck open
    past the retention window be deleted mid-flight — destroying the evidence of an unfulfilled legal
    obligation. Return None (retain) for a non-terminal DSAR so retention can never reach it.
    """
    if collection == "dsar":
        for field in ("completed_at", "resolved_at"):
            value = row.get(field)
            if value:
                return float(value)
        if row.get("status") in ("completed", "refused"):   # the DSAR terminal states
            # terminal but no timestamp field — fall back to created so it can eventually age out
            return float(row.get("created_at") or 0) or None
        return None                              # open/in-flight DSAR — never eligible for deletion
    for field in ("completed_at", "resolved_at", "at", "created_at"):
        value = row.get(field)
        if value:
            return float(value)
    return None
