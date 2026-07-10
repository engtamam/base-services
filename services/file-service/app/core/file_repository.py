"""FileRepository over DataStore — metadata + content-addressed ref-counting."""
from __future__ import annotations

from app.core.models import FileObject
from app.ports.datastore import DataStore

_FILES = "files"
_REFS = "blob_refs"   # sha256 -> {count}
_SCAN = 1_000_000


class FileRepository:
    def __init__(self, store: DataStore) -> None:
        self._store = store

    async def create(self, f: FileObject) -> FileObject:
        await self._store.put(_FILES, f.id, f.to_dict())
        return f

    async def get(self, file_id: str) -> FileObject | None:
        row = await self._store.get(_FILES, file_id)
        return FileObject.from_dict(row) if row else None

    async def list(self, tenant: str, limit: int = 100) -> list[dict]:
        rows = [r for r in await self._store.list(_FILES, _SCAN) if r.get("tenant") == tenant]
        rows.sort(key=lambda r: r.get("created", ""), reverse=True)
        return rows[:limit]

    async def delete(self, file_id: str) -> None:
        await self._store.delete(_FILES, file_id)

    # ── content-addressed ref-count ───────────────────────────────────────────
    async def incr_ref(self, key: str) -> int:
        row = await self._store.get(_REFS, key)
        count = int(row["count"]) + 1 if row else 1
        await self._store.put(_REFS, key, {"count": count})
        return count

    async def decr_ref(self, key: str) -> int:
        row = await self._store.get(_REFS, key)
        count = int(row["count"]) - 1 if row else 0
        if count <= 0:
            await self._store.delete(_REFS, key)
            return 0
        await self._store.put(_REFS, key, {"count": count})
        return count
