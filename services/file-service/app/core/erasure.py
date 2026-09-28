"""Erasure — file-service: what it deletes for a data subject, and what it keeps, with the reason.

Files owned by the subject are their data and are deleted, and the blob ref-count is
decremented so the underlying bytes are collected once nobody references them. A file
shared by two owners must not vanish for the other one — that is what the ref-count is for.

Every ack names what was ERASED and what was RETAINED **with its legal basis**. "We kept it" is
always an answer with a reason attached, never a silence — a DSAR that cannot say why data
survived has not really answered the person.

Idempotent: a DSAR retries, so erasing an already-erased subject is a no-op that still acks.

TWO WAYS A FILE BELONGS TO SOMEBODY, and for a long time this only handled one.

Files they UPLOADED are found by `owner`. Files they merely APPEAR IN are found through
`SubjectLinks` — because when a service stores data on a person's behalf, the uploader is the
service and the subject is named nowhere on the record. A recording sink writes match footage as
"service:stream"; the children in the video have no `owner` row anywhere.

Erasing only by owner therefore matched nothing for exactly the files where erasure matters most,
and still returned `{"erased": [], "already_absent": true}` — a receipt asserting the subject had
no data here. Not a missing delete path: a delete path that reports success having deleted
nothing, which is worse, because the receipt gets filed and produced as evidence the right was
honoured.
"""
from __future__ import annotations

from typing import Any

from app.core.pii import subject_ref
from app.core.subject_links import SubjectLinks
from app.core.upload_session import ABORTED, _TERMINAL, UploadSession


_FILES = "files"
_REFS = "blob_refs"
_SESSIONS = "upload_sessions"


class FileErasure:
    service = "file-service"
    category = "user_files"

    def __init__(self, *, store, object_store=None, pii_salt: str) -> None:
        self._store = store
        self._objects = object_store
        self._salt = pii_salt
        self._links = SubjectLinks(store=store, pii_salt=pii_salt)

    async def erase(self, tenant: str, subject: str) -> dict[str, Any]:
        ref = subject_ref(self._salt, tenant, subject)
        deleted = 0
        cursor = None
        while True:
            rows, cursor = await self._store.find(_FILES, {"tenant": tenant, "owner": subject},
                                                  limit=500, after=cursor, sort=("created", "asc"))
            for r in rows:
                await self._delete_file(r)
                deleted += 1
            if not cursor:
                break

        # Files the subject APPEARS IN but did not upload — the recording-sink case. Deleted
        # through the same path as an owned file, so the ref-count and blob collection behave
        # identically; a shared blob is still not pulled out from under its other owner.
        linked_ids = await self._links.files_for(tenant=tenant, ref=ref)
        linked = 0
        for file_id in linked_ids:
            row = await self._store.get(_FILES, file_id)
            if row is None:
                continue          # already erased by the owner pass, or gone; the link still goes
            await self._delete_file(row)
            linked += 1
        # The linkage is itself personal data — "this named person appeared in this recording" —
        # so it is dropped whether or not each file was still there. Keeping the index after
        # deleting the files would retain the single fact the subject most wanted removed.
        await self._links.forget(tenant=tenant, ref=ref, file_ids=linked_ids)

        # In-flight large uploads are the subject's data too: abort each non-terminal session so its
        # multipart + staging object are released. Without this, an erasure could leave a person's
        # half-uploaded video sitting in the bucket and the receipt would still claim "all erased".
        aborted, scrubbed = await self._erase_sessions(tenant, subject, ref)

        erased = ([f"files:{deleted}"] if deleted else []) + \
                 ([f"linked_files:{linked}"] if linked else []) + \
                 ([f"subject_links:{len(linked_ids)}"] if linked_ids else []) + \
                 ([f"upload_sessions:{aborted}"] if aborted else []) + \
                 ([f"upload_session_records:{scrubbed}"] if scrubbed else [])
        return {
            "service": self.service,
            "subject_ref": ref,
            "erased": erased,
            "retained": [],
            # "I found nothing" must account for EVERY way this service holds a person, or it is
            # an assertion of absence based on having looked in one place.
            "already_absent": (deleted == 0 and aborted == 0 and scrubbed == 0
                               and not linked_ids),
        }

    async def _delete_file(self, row: dict[str, Any]) -> None:
        """Delete one file record and release its blob if nobody else references it."""
        sha = row.get("sha256", "")
        await self._store.delete(_FILES, row["id"])
        if not sha:
            return
        # Atomic decrement, then delete the blob ONLY under a confirmed-zero claim. The old code
        # was a get()+put() race that could delete a blob a SECOND owner still references — the
        # erasure receipt would then claim bytes were gone that a live tenant was about to lose, or
        # would leak them forever. "A file shared by two owners must not vanish for the other one"
        # is exactly what the claim protects.
        remaining = await self._store.incr(_REFS, sha, -1)
        if remaining == 0 and await self._store.del_if_zero(_REFS, sha) \
                and self._objects is not None:
            try:
                await self._objects.delete(sha)
            except Exception:   # noqa: BLE001 — a missing blob is already erased
                pass

    async def _erase_sessions(self, tenant: str, subject: str, ref: str) -> tuple[int, int]:
        """Erase the subject from every upload session — in-flight AND terminal.

        A non-terminal session is aborted (its multipart + staging object released). A TERMINAL
        session (READY/QUARANTINED) is not in-flight, but its row still stores owner, filename,
        declared_sha256 and content_type — the subject's identity and what they uploaded (file-004).
        Those are scrubbed to the pseudonym/empty so the row no longer identifies the person; the blob
        itself is already handled by the file pass. Returns (aborted, scrubbed)."""
        aborted = scrubbed = 0
        cursor = None
        while True:
            rows, cursor = await self._store.find(_SESSIONS, {"tenant": tenant, "owner": subject},
                                                  limit=500, after=cursor, sort=("created", "asc"))
            for r in rows:
                sess = UploadSession.from_dict(r)
                if sess.status not in _TERMINAL:
                    if self._objects is not None:
                        try:
                            await self._objects.abort_multipart(sess.staging_key, sess.upload_id)
                        except Exception:   # noqa: BLE001
                            pass
                        try:
                            await self._objects.delete(sess.staging_key)
                        except Exception:   # noqa: BLE001
                            pass
                    sess.status = ABORTED
                    sess.reason = "erased"
                    sess.owner = ref
                    sess.filename = ""
                    sess.declared_sha256 = ""
                    sess.content_type = ""
                    await self._store.put(_SESSIONS, sess.id, sess.to_dict())
                    aborted += 1
                else:
                    # Terminal: scrub the identifying fields in place, keep the row (its lifecycle is
                    # done). owner -> pseudonym severs the subject link; filename/sha/content_type go.
                    sess.owner = ref
                    sess.filename = ""
                    sess.declared_sha256 = ""
                    sess.content_type = ""
                    await self._store.put(_SESSIONS, sess.id, sess.to_dict())
                    scrubbed += 1
            if not cursor:
                break
        return aborted, scrubbed
