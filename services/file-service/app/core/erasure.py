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


_FILES = "files"
_REFS = "blob_refs"


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
                    # decrement the ref-count; only drop the blob when nobody else points at it
                    refs = await self._store.get(_REFS, sha)
                    count = int((refs or {}).get("count", 1)) - 1
                    if count <= 0:
                        await self._store.delete(_REFS, sha)
                        if self._objects is not None:
                            try:
                                await self._objects.delete(sha)
                            except Exception:   # noqa: BLE001 — a missing blob is already erased
                                pass
                    else:
                        await self._store.put(_REFS, sha, {"count": count})
            if not cursor:
                break

        return {
            "service": self.service,
            "subject_ref": ref,
            "erased": [f"files:{deleted}"] if deleted else [],
            "retained": [],
            "already_absent": deleted == 0,
        }
