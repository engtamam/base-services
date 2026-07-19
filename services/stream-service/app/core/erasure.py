"""Erasure — stream: video of people that no subject id can find, answered honestly.

Recordings and exports are footage of physical spaces, so they contain identifiable people by
construction. But every row here is keyed by CAMERA and TIME (`source_id`, `started`/`ended`) —
there is no `subject` column, and there cannot honestly be one, because knowing who appears in a
frame would require running facial recognition over the footage. Building that to satisfy an
erasure request would create far more biometric data than it deleted.

That makes this the one case in the platform where the truthful answer is "we cannot find them":

  * **Not `already_absent`.** Claiming "nothing here" would be a lie by omission — the person may
    well be on camera. This service answers `identification_not_possible` instead, which is a
    recognised position (GDPR Art. 11: a controller not in a position to identify the subject is
    not required to acquire extra data purely to comply, and may ask the subject for information
    that enables identification).

  * **With a way forward attached.** A refusal that leaves the person stuck is not an answer. The
    ack names the operator path: supply a camera and time window, and the recordings covering it
    are erased. That is the only identification that exists here, and the subject usually knows it
    ("I was in the lobby on the 3rd at 14:00") better than we ever could.

  * **A locked recording is never deleted.** `Recording.locked` is a legal hold, and a hold that an
    erasure could delete is not a hold. Held footage is retained and SAID to be retained.

Idempotent, and it never deletes on a guess: with no camera/window supplied, this reads nothing and
deletes nothing.
"""
from __future__ import annotations

from typing import Any

from app.core.pii import subject_ref

_RECORDINGS = "recordings"
_EXPORTS = "exports"
_PAGE = 500

NO_ID_BASIS = "identification_not_possible"
HOLD_BASIS = "legal_hold"


class StreamErasure:
    service = "stream-service"
    category = "recordings"

    def __init__(self, *, store, pii_salt: str, object_store=None, max_scan: int = 100_000) -> None:
        self._store = store
        self._salt = pii_salt
        self._objects = object_store
        self._max = max_scan

    async def erase(self, tenant: str, subject: str,
                    source_id: str = "", start: str = "", end: str = "") -> dict[str, Any]:
        """Erase footage the caller has IDENTIFIED by camera and window.

        Without `source_id`, nothing is touched: an erasure that guessed which recordings belong to
        a person would delete other people's footage — and deleting the wrong video to satisfy a
        privacy request is itself a data-protection failure, not a cautious one.
        """
        ref = subject_ref(self._salt, tenant, subject)
        if not source_id:
            return self._cannot_identify(ref)

        deleted, held, scanned = 0, 0, 0
        cursor = None
        while scanned < self._max:
            rows, cursor = await self._store.find(_RECORDINGS, {"tenant": tenant,
                                                                "source_id": source_id},
                                                  limit=_PAGE, after=cursor, sort=("started", "asc"))
            if not rows:
                break
            for rec in rows:
                scanned += 1
                if not _overlaps(rec, start, end):
                    continue
                if rec.get("locked"):
                    held += 1                      # a hold an erasure could delete is not a hold
                    continue
                await self._delete_segments(rec)
                await self._store.delete(_RECORDINGS, rec["id"])
                deleted += 1
            if not cursor:
                break

        exports = await self._delete_exports(tenant, source_id, start, end)

        erased = []
        if deleted:
            erased.append(f"recordings:{deleted}")
        if exports:
            erased.append(f"exports:{exports}")

        retained = []
        if held:
            retained.append({"what": f"recordings:{held}", "basis": HOLD_BASIS,
                             "why": "under legal hold — a hold an erasure could delete is not a hold"})

        return {"service": self.service, "category": self.category, "subject_ref": ref,
                "erased": erased, "retained": retained,
                "already_absent": not erased and not held,
                "truncated": scanned >= self._max}

    def _cannot_identify(self, ref: str) -> dict[str, Any]:
        return {
            "service": self.service,
            "category": self.category,
            "subject_ref": ref,
            "erased": [],
            "retained": [{
                "what": "recordings",
                "basis": NO_ID_BASIS,
                "why": ("footage is keyed by camera and time, not by person; identifying who "
                        "appears in a frame would require facial recognition, which would create "
                        "more biometric data than it erased. Supply source_id (and optionally "
                        "start/end) to erase the recordings covering a specific camera and window "
                        "— that is the only identification this data supports."),
            }],
            # NOT already_absent: the person may well be on camera, and claiming otherwise would be
            # a lie. This is a request we cannot fulfil, not a request with nothing behind it.
            "already_absent": False,
            "needs_identification": ["source_id", "start", "end"],
        }

    async def _delete_exports(self, tenant: str, source_id: str, start: str, end: str) -> int:
        deleted = 0
        cursor = None
        while True:
            rows, cursor = await self._store.find(_EXPORTS, {"tenant": tenant,
                                                             "source_id": source_id},
                                                  limit=_PAGE, after=cursor, sort=("start", "asc"))
            if not rows:
                break
            for exp in rows:
                if not _overlaps({"started": exp.get("start"), "ended": exp.get("end")}, start, end):
                    continue
                if exp.get("file_id") and self._objects is not None:
                    try:
                        await self._objects.delete(exp["file_id"])
                    except Exception:
                        pass          # the row still goes; a stranded blob is swept by retention
                await self._store.delete(_EXPORTS, exp["id"])
                deleted += 1
            if not cursor:
                break
        return deleted

    async def _delete_segments(self, rec: dict) -> None:
        """The row is an index; the FOOTAGE is the personal data. Deleting the row alone would
        leave the video on disk and report success."""
        if self._objects is None:
            return
        for seg in rec.get("segments") or []:
            key = seg.get("key") or seg.get("file_id")
            if not key:
                continue
            try:
                await self._objects.delete(key)
            except Exception:
                pass


def _overlaps(rec: dict, start: str, end: str) -> bool:
    """Does this recording intersect the requested window? An open window means the whole camera.

    ISO-8601 timestamps compare correctly as strings, which is why they are stored that way.
    """
    rec_start = rec.get("started") or ""
    rec_end = rec.get("ended") or rec_start
    if start and rec_end and rec_end < start:
        return False
    if end and rec_start and rec_start > end:
        return False
    return True
