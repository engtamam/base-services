"""ObjectStore port — blob storage (files, media)."""
from __future__ import annotations

from typing import Protocol, runtime_checkable


@runtime_checkable
class ObjectStore(Protocol):
    driver: str

    async def put(self, key: str, data: bytes, content_type: str | None = None) -> None: ...

    async def get(self, key: str) -> bytes:
        """Return the object bytes, or raise NotFound."""
        ...

    async def delete(self, key: str) -> bool: ...

    async def exists(self, key: str) -> bool: ...

    async def health(self) -> bool: ...

    async def close(self) -> None: ...
