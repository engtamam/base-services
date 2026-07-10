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

    async def cas(self, collection: str, key: str, expected_version: int,
                  value: dict[str, Any]) -> bool:
        """Compare-and-swap on `value["version"]`. True if written, False if someone raced us.

        A wallet spend mutates SEVERAL funds at once and appends a ledger row, so a
        single-counter increment cannot express it. `get` then `put` is a lost update: two
        spenders read the same balance and the second overwrites the first, giving away money.

        `expected_version=0` means "create only if absent".
        The caller re-reads and retries on False.
        """
        ...

    async def health(self) -> bool: ...

    async def close(self) -> None: ...
