"""DataStore port — generic record persistence (repository backing)."""
from __future__ import annotations

from typing import Any, Protocol, runtime_checkable


@runtime_checkable
class DataStore(Protocol):
    driver: str

    async def get(self, collection: str, key: str) -> dict[str, Any] | None: ...

    async def put(self, collection: str, key: str, value: dict[str, Any]) -> None: ...

    async def delete(self, collection: str, key: str) -> bool: ...

    async def list(self, collection: str, limit: int = 100) -> list[dict[str, Any]]: ...

    async def health(self) -> bool: ...

    async def close(self) -> None: ...
