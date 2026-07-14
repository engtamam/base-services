"""FileRepository over DataStore — metadata + content-addressed ref-counting."""
from __future__ import annotations

from app.core.models import FileObject
from app.ports.datastore import DataStore

_FILES = "files"
_REFS = "blob_refs"   # sha256 -> {count}

# There is deliberately no _SCAN constant any more. It was 1_000_000: not a limit, a scan
# wearing a limit's clothes. Every row of every tenant crossed the process boundary so that a
# hundred could be returned — and past the cap the answer was silently, arbitrarily wrong.


class FileRepository:
    def __init__(self, store: DataStore) -> None:
        self._store = store

    async def create(self, f: FileObject) -> FileObject:
        await self._store.put(_FILES, f.id, f.to_dict())
        return f

    async def get(self, file_id: str) -> FileObject | None:
        row = await self._store.get(_FILES, file_id)
        return FileObject.from_dict(row) if row else None

    async def list(self, tenant: str, limit: int = 100,
                   after: str | None = None) -> tuple[list[dict], str | None]:
        """One tenant's files, newest first. The filter is pushed into the store.

        Cost is proportional to what is RETURNED, not to what everyone else has stored.
        """
        return await self._store.find(_FILES, {"tenant": tenant}, limit=limit, after=after,
                                      sort=("created", "desc"))

    async def delete(self, file_id: str) -> None:
        await self._store.delete(_FILES, file_id)

    # ── content-addressed ref-count ───────────────────────────────────────────
    async def incr_ref(self, key: str) -> int:
        """One more owner points at this blob. Atomic — no lost increment under concurrent upload."""
        return await self._store.incr(_REFS, key, +1)

    async def decr_ref(self, key: str) -> int:
        """One fewer owner. Atomic, and never negative. Returns the remaining count.

        Note it does NOT delete the blob at zero — the caller must gate that on `claim_blob_delete`,
        so a concurrent uploader that re-references the blob keeps it alive.
        """
        return await self._store.incr(_REFS, key, -1)

    async def claim_blob_delete(self, key: str) -> bool:
        """May I delete this blob? True only if the ref-count is atomically confirmed zero AND this
        call is the one that removed the row. A concurrent re-upload makes it False, so the bytes
        an owner still references are never deleted."""
        return await self._store.del_if_zero(_REFS, key)
