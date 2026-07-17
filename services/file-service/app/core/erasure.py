"""Erasure — file-service: what it deletes for a data subject, and what it keeps, with the reason.

Files owned by the subject are their data and are deleted, and the blob ref-count is
decremented so the underlying bytes are collected once nobody references them. A file
shared by two owners must not vanish for the other one — that is what the ref-count is for.

Every ack names what was ERASED and what was RETAINED **with its legal basis**. "We kept it" is
always an answer with a reason attached, never a silence — a DSAR that cannot say why data
survived has not really answered the person.

Idempotent: a DSAR retries, so erasing an already-erased subject is a no-op that still acks.
"""
from __future__ import annotations

from typing import Any

from app.core.pii import subject_ref
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

    async def erase(self, tenant: str, subject: str) -> dict[str, Any]:
        ref = subject_ref(self._salt, tenant, subject)
        deleted = 0
        cursor = None
        while True:
            rows, cursor = await self._store.find(_FILES, {"tenant": tenant, "owner": subject},
                                                  limit=500, after=cursor, sort=("created", "asc"))
            for r in rows:
                sha = r.get("sha256", "")
                await self._store.delete(_FILES, r["id"])
                deleted += 1
                if sha:
                    # Atomic decrement, then delete the blob ONLY under a confirmed-zero claim. The
                    # old code was a get()+put() race that could delete a blob a SECOND owner still
                    # references — the erasure receipt would then claim bytes were gone that a live
                    # tenant was about to lose, or would leak them forever. "A file shared by two
                    # owners must not vanish for the other one" is exactly what the claim protects.
                    remaining = await self._store.incr(_REFS, sha, -1)
                    if remaining == 0 and await self._store.del_if_zero(_REFS, sha) \
                            and self._objects is not None:
                        try:
                            await self._objects.delete(sha)
                        except Exception:   # noqa: BLE001 — a missing blob is already erased
                            pass
            if not cursor:
                break

        # In-flight large uploads are the subject's data too: abort each non-terminal session so its
        # multipart + staging object are released. Without this, an erasure could leave a person's
        # half-uploaded video sitting in the bucket and the receipt would still claim "all erased".
        aborted = await self._erase_sessions(tenant, subject)

        erased = ([f"files:{deleted}"] if deleted else []) + \
                 ([f"upload_sessions:{aborted}"] if aborted else [])
        return {
            "service": self.service,
            "subject_ref": ref,
            "erased": erased,
            "retained": [],
            "already_absent": deleted == 0 and aborted == 0,
        }

    async def _erase_sessions(self, tenant: str, subject: str) -> int:
        aborted = 0
        cursor = None
        while True:
            rows, cursor = await self._store.find(_SESSIONS, {"tenant": tenant, "owner": subject},
                                                  limit=500, after=cursor, sort=("created", "asc"))
            for r in rows:
                sess = UploadSession.from_dict(r)
                if sess.status not in _TERMINAL and self._objects is not None:
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
                    await self._store.put(_SESSIONS, sess.id, sess.to_dict())
                    aborted += 1
            if not cursor:
                break
        return aborted
