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

    def __init__(self, *, store, pii_salt: str, sink=None, object_store=None,
                 max_scan: int = 100_000) -> None:
        self._store = store
        self._salt = pii_salt
        # The SINK is where a segment's bytes actually live — memory, a local dir, s3, or
        # file-service — and it is the only thing that knows how to delete by the `ref` it handed
        # back. Erasure used to delete through `object_store` instead, which for the file-service
        # sink is the WRONG store entirely (the bytes are in file-service, not our bucket), and it
        # read the wrong field on top of that. `object_store` is kept only for the exceptional
        # deployment that has no sink; when a sink is present it wins.
        self._sink = sink
        self._objects = object_store
        self._max = max_scan

    async def _delete_ref(self, ref: str) -> None:
        """Delete one stored object by the ref the sink handed back. Sink first — it is where the
        bytes are; object_store is a fallback for a sink-less deployment. Best-effort: a stranded
        blob is swept by retention, but a MISSING delete path is the false-receipt bug."""
        if not ref:
            return
        try:
            if self._sink is not None:
                await self._sink.delete(ref)
            elif self._objects is not None:
                await self._objects.delete(ref)
        except Exception:   # noqa: BLE001 — a blob already gone is already erased
            pass

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
                # An export's clip is stored through the SAME sink as a segment (exporting sets
                # exp.file_id = ref.ref from sink.put_segment), so it is deleted the same way. It
                # too was going through object_store — wrong store for the file-service sink.
                await self._delete_ref(exp.get("file_id"))
                await self._store.delete(_EXPORTS, exp["id"])
                deleted += 1
            if not cursor:
                break
        return deleted

    async def _delete_segments(self, rec: dict) -> None:
        """The row is an index; the FOOTAGE is the personal data. Deleting the row alone would
        leave the video where it lives and report success.

        A segment carries its object id under `ref` — that is what the sink returns from
        put_segment and what export reads to build a download URL. The old code looked for `key`
        or `file_id`, which no segment has, so the loop deleted nothing on every recording, the
        row was removed anyway, and the receipt said `recordings:N`. Footage kept, erasure
        reported. That is the same false-erasure-receipt this platform has now been bitten by
        twice; here it is a child's match video staying in file-service after a deletion request.
        """
        for seg in rec.get("segments") or []:
            await self._delete_ref(seg.get("ref"))


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
