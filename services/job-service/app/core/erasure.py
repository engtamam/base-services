"""Erasure — job: the person is in the work itself, so erasing has to stop the work too.

A job's `payload` is its arguments and its `result` is its output. A "send welcome email" job holds
the recipient's address; the result of an export job may hold their data outright. Neither has a
subject column, so this walks the blob (see blob.py) exactly as webhook does.

The part that is specific to a QUEUE, and the reason this file is not a copy of webhook's:

    **A queued job that exists to contact the person must be cancelled, not just redacted.**

Redacting the payload of a job that has not run yet leaves a job that will run — against a
pseudonym, doing something nobody wants — or, worse, a job whose payload was only partly matched
and which still reaches the person we were told to forget. An erasure that leaves work scheduled
against the erased subject has not erased anything; it has queued one more contact with them.

So: queued and leased jobs that mention the subject are CANCELED and redacted. Finished jobs
(done/dead/canceled) are redacted in place, because their history is operational record and only
the person needs to leave it. Schedules — recurring work — are cancelled on the same reasoning:
a cron that emails a deleted user every month is the most visible possible failure of a DSAR.

Idempotent: after the first pass nothing matches, so a retry finds nothing and still acks.
"""
from __future__ import annotations

from typing import Any

from app.core.blob import redact
from app.core.models import ACTIVE, CANCELED
from app.core.pii import subject_ref

_JOBS = "jobs"
_SCHEDULES = "schedules"
_DUE = "jobs_due"          # the claimable index — must not outlive a job canceled by erasure (job-007)
_LEASED = "jobs_leased"    # the in-flight index — same
_PAGE = 500


class JobErasure:
    service = "job-service"
    category = "job_payloads"

    def __init__(self, *, store, pii_salt: str, text_keys: frozenset[str] = frozenset(),
                 max_scan: int = 100_000) -> None:
        self._store = store
        self._salt = pii_salt
        self._text_keys = text_keys
        self._max = max_scan

    async def erase(self, tenant: str, subject: str) -> dict[str, Any]:
        ref = subject_ref(self._salt, tenant, subject)
        redacted, canceled, truncated = 0, 0, False

        for collection, fields in ((_JOBS, ("payload", "result")), (_SCHEDULES, ("payload",))):
            r, c, cut = await self._walk(collection, fields, tenant, subject, ref)
            redacted += r
            canceled += c
            truncated = truncated or cut

        erased = []
        if redacted:
            erased.append(f"job_payload_fields:{redacted}")
        if canceled:
            # Reported as ERASED work, not as a side effect: cancelling scheduled contact with the
            # person is part of erasing them, and the ack should say it happened.
            erased.append(f"canceled_pending_work:{canceled}")

        return {
            "service": self.service,
            "category": self.category,
            "subject_ref": ref,
            "erased": erased,
            "retained": [],          # job history carries no legal duty of its own
            "already_absent": not erased,
            "truncated": truncated,
        }

    async def _walk(self, collection: str, fields: tuple[str, ...], tenant: str, subject: str,
                    ref: str) -> tuple[int, int, bool]:
        redacted = canceled = scanned = 0
        cursor = None
        while scanned < self._max:
            rows, cursor = await self._store.find(collection, {"tenant": tenant}, limit=_PAGE,
                                                  after=cursor, sort=("created", "asc"))
            if not rows:
                break
            for row in rows:
                scanned += 1
                hits = 0
                for field in fields:
                    if field not in row:
                        continue
                    new, n = redact(row[field], subject, ref, self._text_keys, _key=field)
                    if n:
                        row[field] = new
                        hits += n
                if not hits:
                    continue
                # Stop work that has not happened yet. A redacted-but-still-queued job either runs
                # against a pseudonym or still reaches the person we were told to forget.
                if _is_pending(row, collection):
                    row["status"] = CANCELED
                    row["cancel_reason"] = "erasure: the data subject was erased"
                    canceled += 1
                    if collection == _SCHEDULES:
                        row["enabled"] = False
                    elif collection == _JOBS:
                        # job-007: a blind status=CANCELED put left the due/leased INDEX rows behind, so a
                        # claim/reclaim could still pick the job up (running erased work) and stats still
                        # counted it. Drop both index entries, as JobService.cancel does.
                        await self._store.delete(_DUE, row["id"])
                        await self._store.delete(_LEASED, row["id"])
                await self._store.put(collection, row["id"], row)
                redacted += hits
            if not cursor:
                break
        return redacted, canceled, scanned >= self._max


def _is_pending(row: dict, collection: str) -> bool:
    if collection == _SCHEDULES:
        return bool(row.get("enabled", True))
    return row.get("status") in ACTIVE
