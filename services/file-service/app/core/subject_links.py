"""Who is IN a file, as opposed to who uploaded it.

Erasure used to find a person's files with

    find(files, {"tenant": tenant, "owner": subject})

which is right only while the uploader and the data subject are the same person. They are not,
whenever a SERVICE stores something on someone's behalf: a recording sink writes match footage
authenticated as itself, so `owner` is "service:stream" and the children in the video are named
nowhere. The erasure then runs, matches nothing, and returns a receipt saying the subject's files
are gone.

THAT IS THE PART THAT MATTERS. Not a missing delete path — a delete path that reports success
having deleted nothing. A missing endpoint gets noticed the first time somebody tries to use it.
A false receipt is filed, relied on, and produced to a regulator as evidence that a right was
honoured. It is the same shape as an erasure that silently does not run, except it also
manufactures the proof.

So a file carries an explicit set of SUBJECTS: the people whose personal data it contains,
independent of who uploaded it.

WHY A COLLECTION AND NOT A FIELD. The obvious form is `FileObject.subjects: list[str]`, and it
cannot be queried: `find()` filters by equality, so "every file whose subjects list CONTAINS this
person" would have to scan the table — forbidden on any request path, and erasure is one. A
linkage record per (subject, file) keeps the lookup an indexed equality read.

WHAT IS STORED IS THE PSEUDONYM, never the person. `subject_ref` is already how this platform
names people in records that outlive them, and a linkage table full of raw email addresses would
be a new copy of exactly the data erasure exists to remove.

LINKAGE ARRIVES LATE, AND THAT IS NORMAL. A recording is stored before anyone knows who is in it;
identification happens afterwards, sometimes only once a coach names a face and consent is checked.
So subjects can be attached to a file that already exists, and attaching is idempotent.
"""
from __future__ import annotations

from typing import Any

from app.core.pii import subject_ref

_LINKS = "file_subjects"


def link_id(tenant: str, ref: str, file_id: str) -> str:
    """Deterministic id, so attaching the same subject twice is one record, not two.

    Idempotence is not a nicety here. Identification is retried — a job re-runs, a coach re-tags
    the same face — and duplicate links would make an erasure receipt count one file three times,
    which is a different way of lying about what was deleted.
    """
    return f"{tenant}|{ref}|{file_id}"


class SubjectLinks:
    """The record that a given person appears in a given file."""

    def __init__(self, *, store, pii_salt: str) -> None:
        self._store = store
        self._salt = pii_salt

    async def attach(self, *, tenant: str, file_id: str, subjects: list[str]) -> int:
        """Record that each subject appears in this file. Returns how many links now exist."""
        if not tenant or not file_id:
            raise ValueError("tenant and file_id are required to link a subject")
        n = 0
        for subject in subjects:
            if not subject:
                continue
            ref = subject_ref(self._salt, tenant, subject)
            await self._store.put(_LINKS, link_id(tenant, ref, file_id),
                                  {"tenant": tenant, "subject_ref": ref, "file_id": file_id})
            n += 1
        return n

    async def files_for(self, *, tenant: str, ref: str, limit: int = 500) -> list[str]:
        """Every file this pseudonym is linked to. An indexed equality read, never a scan."""
        out: list[str] = []
        cursor = None
        while True:
            rows, cursor = await self._store.find(
                _LINKS, {"tenant": tenant, "subject_ref": ref},
                limit=limit, after=cursor, sort=("file_id", "asc"))
            out.extend(r["file_id"] for r in rows if r.get("file_id"))
            if not cursor:
                break
        return out

    async def forget(self, *, tenant: str, ref: str, file_ids: list[str]) -> None:
        """Drop the linkage itself once the files are gone.

        The linkage is personal data too — it says a named person appeared in a recording — so an
        erasure that deleted the files and kept the index would leave behind the one fact the
        subject most likely wanted removed.
        """
        for file_id in file_ids:
            await self._store.delete(_LINKS, link_id(tenant, ref, file_id))

def collection() -> str:
    return _LINKS
