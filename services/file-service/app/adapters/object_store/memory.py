"""In-memory ObjectStore — zero infra."""
from __future__ import annotations

from typing import AsyncIterator

from app.adapters.object_store.keys import validate_key
from app.ports.errors import NotFound, Unsupported
from app.ports.object_store import CompletedPart, MultipartPlan, ObjectHead


class MemoryObjectStore:
    driver = "memory"
    supports_presign = False   # no bucket to presign against → SMALL path only

    def __init__(self) -> None:
        self._data: dict[str, bytes] = {}

    async def put(self, key: str, data: bytes, content_type: str | None = None) -> None:
        self._data[validate_key(key)] = bytes(data)

    async def get(self, key: str) -> bytes:
        k = validate_key(key)
        if k not in self._data:
            raise NotFound(f"object not found: {k}")
        return self._data[k]

    async def delete(self, key: str) -> bool:
        return self._data.pop(validate_key(key), None) is not None

    async def exists(self, key: str) -> bool:
        return validate_key(key) in self._data

    async def health(self) -> bool:
        return True

    # ── large path: not available (no bucket to hand the client) ──────────────
    async def begin_multipart(self, key: str, content_type: str, size: int,
                              part_size: int) -> MultipartPlan:
        raise Unsupported("memory object store cannot presign — use an s3 store for large uploads")

    async def complete_multipart(self, key: str, upload_id: str,
                                 parts: list[CompletedPart]) -> None:
        raise Unsupported("memory object store cannot presign")

    async def abort_multipart(self, key: str, upload_id: str) -> None:
        raise Unsupported("memory object store cannot presign")

    async def presign_get(self, key: str, filename: str, expires_in: int) -> str:
        raise Unsupported("memory object store cannot presign")

    async def head(self, key: str) -> ObjectHead | None:
        k = validate_key(key)
        blob = self._data.get(k)
        return None if blob is None else ObjectHead(size=len(blob), etag="")

    async def promote(self, src_key: str, dst_key: str) -> None:
        src, dst = validate_key(src_key), validate_key(dst_key)
        if dst not in self._data and src in self._data:
            self._data[dst] = self._data[src]
        self._data.pop(src, None)

    async def stream(self, key: str, chunk_size: int = 1_048_576) -> AsyncIterator[bytes]:
        k = validate_key(key)
        if k not in self._data:
            raise NotFound(f"object not found: {k}")
        data = self._data[k]
        for i in range(0, len(data), max(1, chunk_size)):
            yield data[i:i + chunk_size]

    async def close(self) -> None:
        self._data.clear()
