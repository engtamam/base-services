"""Erasure — analytics-service: what it deletes for a data subject, and what it keeps, with the reason.

Raw events are keyed by the subject and ARE personal data — they are deleted.
The rollups are not: they are pre-aggregated counts with no subject in them, so there is
nothing personal left to erase and nothing to declare. Deleting the aggregate would
corrupt every other tenant's metrics for no privacy gain.

Every ack names what was ERASED and what was RETAINED **with its legal basis**. "We kept it" is
always an answer with a reason attached, never a silence — a DSAR that cannot say why data
survived has not really answered the person.

Idempotent: a DSAR retries, so erasing an already-erased subject is a no-op that still acks.
"""
from __future__ import annotations

from typing import Any

from app.core.pii import subject_ref
from app.core.stores import seen_key


_EVENTS = "events"
_SEEN = "event_seen"


class AnalyticsErasure:
    service = "analytics-service"
    category = "behavioural_events"

    def __init__(self, *, store, pii_salt: str) -> None:
        self._store = store
        self._salt = pii_salt

    async def erase(self, tenant: str, subject: str) -> dict[str, Any]:
        ref = subject_ref(self._salt, tenant, subject)
        # Events store the subject as its salted, tenant-scoped pseudonym (ingest pseudonymises
        # before writing), so we MUST search by the pseudonym, not the raw identifier — searching
        # raw would match zero rows in production and falsely ack "already absent". A missing salt
        # (dev/test only) means ingest stored the raw subject, so fall back to it there.
        needle = ref if self._salt else subject
        deleted = 0
        cursor = None
        while True:
            rows, cursor = await self._store.find(_EVENTS, {"tenant": tenant, "subject": needle},
                                                  limit=500, after=cursor, sort=("event_time", "asc"))
            for r in rows:
                await self._store.delete(_EVENTS, r["id"])
                await self._store.delete(_SEEN, seen_key(tenant, r["id"]))
                deleted += 1
            if not cursor:
                break

        return {
            "service": self.service,
            "subject_ref": ref,
            "erased": [f"events:{deleted}"] if deleted else [],
            # Rollups are retained: they hold only the salted, tenant-scoped subject_ref (never the
            # raw identifier), and erasing this subject destroys the reverse linkage held elsewhere,
            # so the aggregate can no longer name anybody. Deleting the aggregate would corrupt every
            # other subject's metrics for no privacy gain. Legal basis: pseudonymised aggregate.
            "retained": ["rollups:pseudonymised_aggregate"],
            "already_absent": deleted == 0,
        }
