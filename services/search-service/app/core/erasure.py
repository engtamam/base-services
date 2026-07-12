"""Erasure — search-service: what it deletes for a data subject, and what it keeps, with the reason.

The index is a DERIVED store, and derived stores are the leak everybody forgets: delete
the source row and the index still answers the person's name to anyone who searches for it.
So the subject's documents are removed from the index. Nothing is retained — an index can
always be rebuilt from a source that no longer holds the person.

Every ack names what was ERASED and what was RETAINED **with its legal basis**. "We kept it" is
always an answer with a reason attached, never a silence — a DSAR that cannot say why data
survived has not really answered the person.

Idempotent: a DSAR retries, so erasing an already-erased subject is a no-op that still acks.
"""
from __future__ import annotations

from typing import Any

from app.core.pii import subject_ref


_DOCS = "documents"


class SearchErasure:
    service = "search-service"
    category = "search_index"

    def __init__(self, *, store, pii_salt: str) -> None:
        self._store = store
        self._salt = pii_salt

    async def erase(self, tenant: str, subject: str) -> dict[str, Any]:
        ref = subject_ref(self._salt, tenant, subject)
        deleted = 0
        cursor = None
        while True:
            rows, cursor = await self._store.find(_DOCS, {"tenant": tenant, "owner": subject},
                                                  limit=500, after=cursor, sort=("created", "asc"))
            for r in rows:
                await self._store.delete(_DOCS, r["id"])
                deleted += 1
            if not cursor:
                break

        return {
            "service": self.service,
            "subject_ref": ref,
            "erased": [f"documents:{deleted}"] if deleted else [],
            "retained": [],   # a derived index is rebuildable; it may keep nothing
            "already_absent": deleted == 0,
        }
