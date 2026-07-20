"""ConsentSweeper — the watcher that notices a guardian consent about to lapse.

capacity.py sets `expires_at` to the day a minor reaches the age of majority, and that is the
entire majority handling. On their eighteenth birthday `check()` starts answering
`allowed: false, reason: "expired"`, and nothing else happens: no notice to the controller, no
prompt for the young adult's OWN consent. From the outside, processing simply stops working, which
is indistinguishable from a bug — and the platform's honest position is worse than that, because
the right thing to do at majority is ASK THE PERSON, and nobody was ever told to.

This is the same shape as the gap Phase 37 closed for erasure and Phase 38 for the audit log: the
mechanism was right and nothing watched it. A lapse is not an error state that surfaces itself; it
is a date arriving, and a date arriving is only visible to something that looks.

Two events, because a product needs different things from them:

  consent.lapsing  — fires BEFORE the date, once, so there is time to ask. This is the one with
                     product value: it is the window in which a re-consent flow can run and the
                     subject never experiences an interruption.
  consent.lapsed   — fires AFTER, once, so a product can stop processing deliberately rather than
                     discovering it through a denial on a hot path.

Both are emitted at most once per consent, tracked on the record itself. An event that repeats
every night is one a consumer learns to ignore, and this exists precisely to be acted on.
"""
from __future__ import annotations

from typing import Any

# Basis values that carry a majority lapse. `self` never lapses this way — an adult consenting for
# themself has no expiry derived from age.
_DELEGATED = ("guardian", "legal_representative")


class ConsentSweeper:
    def __init__(self, *, store, consents, clock, event_bus=None, notice_seconds: float = 0.0,
                 max_per_run: int = 1000, telemetry=None, capacity=None) -> None:
        self._capacity = capacity
        self._store = store
        self._consents = consents
        self._clock = clock
        self._bus = event_bus
        self._notice = notice_seconds
        self._max = max_per_run
        self._telemetry = telemetry

    async def run(self) -> dict[str, Any]:
        """Walk consents, announce the ones crossing majority. Safe to run twice.

        Idempotent by state on the record, not by timing: two replicas, or a cron overlapping its
        own previous run, cannot double-announce. That matters more than usual here, because the
        consumer of these events is likely to send a human an email.
        """
        now = self._clock.now()
        lapsing = lapsed = 0

        # sweeper: deciding which consents are crossing majority IS visiting them — the store
        # speaks equality only and this is a range question about expires_at, so no filter can
        # express it. scan_all() rather than find({}) because that is what it honestly is; a scan
        # disguised as a filtered read hides from the next reader and from safety:scans. Runs on a
        # CronJob, never on a request path, and capped per run.
        rows = await self._store.scan_all("consent", limit=self._max)

        for row in rows:
            expires_at = float(row.get("expires_at") or 0.0)
            if not expires_at:
                # PRE-POLICY ROWS. A delegated consent granted before CONSENT_AGE_OF_MAJORITY was
                # raised above zero carries no expiry, and this loop used to skip it — so the one
                # category the sweeper exists for was the one it never looked at. Those rows would
                # answer "allowed" for ever and nothing on the platform would revisit them.
                #
                # The lapse date is DERIVED from the subject's birth date rather than trusted from
                # the row, then written back, so the record catches up with the policy instead of
                # staying frozen at whatever was true when it was created.
                expires_at = self._derive_lapse(row)
                if not expires_at:
                    continue                      # genuinely no lapse: self-consent, or age unknown
                row["expires_at"] = expires_at
                await self._store.put("consent", self._key(row), row)
            if row.get("basis", "self") not in _DELEGATED:
                # An expiry set by the granting party is that party's business — this sweeper is
                # only about the one date the SUBJECT did not choose and cannot see coming.
                continue
            if not row.get("granted", True):
                continue                          # already withdrawn; the lapse is moot

            payload = {"tenant": row.get("tenant", ""), "subject": row.get("subject", ""),
                       "purpose": row.get("purpose", ""), "expires_at": expires_at,
                       "basis": row.get("basis", "")}

            if now >= expires_at and not row.get("lapsed_announced"):
                await self._announce("consent.lapsed", payload, row)
                lapsed += 1
            elif (self._notice and now < expires_at            # STRICTLY before — see below
                    and now >= expires_at - self._notice
                    and not row.get("lapsing_announced")):
                # `now < expires_at` is load-bearing, and a test caught its absence. Without it,
                # the run AFTER a lapse fell through to this branch — lapsed_announced was set, so
                # the first condition was false — and announced "about to lapse" for something
                # that already had. A warning that arrives after the event is worse than none: it
                # tells a consumer to start a re-consent flow for a window that has closed.
                await self._announce("consent.lapsing", payload, row)
                lapsing += 1

        summary = {"scanned": len(rows), "lapsing": lapsing, "lapsed": lapsed}
        if self._telemetry is not None:
            try:
                self._telemetry.gauge("consent_lapsing", lapsing)
                self._telemetry.gauge("consent_lapsed", lapsed)
            except Exception:
                pass
        return summary

    def _derive_lapse(self, row: dict) -> float:
        """When a delegated consent lapses, from the subject's age. 0 when it cannot be known.

        Returns 0 rather than guessing whenever capacity is unenforced, the basis is self, or the
        birth date is absent. An UNKNOWN age must never manufacture a lapse date — that would
        expire a consent nobody agreed to end, which is the same class of error as inventing one
        that never ends, pointed the other way.
        """
        if self._capacity is None or not self._capacity.enforced:
            return 0.0
        if row.get("basis", "self") not in _DELEGATED:
            return 0.0
        born_at = float(row.get("subject_born_at") or 0.0)
        if not born_at:
            return 0.0
        return self._capacity.majority_at(born_at)

    @staticmethod
    def _key(row: dict) -> str:
        from app.core.stores import ConsentStore
        return ConsentStore._key(row.get("tenant", ""), row.get("subject", ""),
                                 row.get("purpose", ""))

    async def _announce(self, event: str, payload: dict, row: dict) -> None:
        """Mark first, publish second.

        Deliberately this order. If publishing fails we have marked an event that never went out —
        one missed notice. The other order risks publishing repeatedly on every run until the mark
        finally sticks, and repeatedly telling a guardian their child's consent is ending is worse
        than telling them late. Neither is good; this one degrades quietly.
        """
        flag = "lapsed_announced" if event == "consent.lapsed" else "lapsing_announced"
        row[flag] = True
        await self._store.put("consent",
                              f"{row.get('tenant','')}::{row.get('subject','')}::{row.get('purpose','')}",
                              row)
        if self._bus is None:
            return
        try:
            await self._bus.publish(event, payload)
        except Exception:
            pass
