"""In-memory ObjectStore — zero infra."""
from __future__ import annotations

from app.adapters.object_store.keys import validate_key
from app.ports.errors import Unsupported, NotFound


class MemoryObjectStore:
    driver = "memory"
    supports_presign = False   # no bucket URL a client could resolve

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

    async def presign_get(self, key: str, expires_in: int) -> str:
        raise Unsupported("memory object store cannot presign — use an s3 store for segment download")

    async def health(self) -> bool:
        return True

    async def close(self) -> None:
        self._data.clear()
