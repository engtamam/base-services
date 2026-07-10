"""DataStore port — generic record persistence (repository backing).

`incr_if_below` exists because get+put cannot enforce a quota. Two pods both read `24`,
both write `25`, and the 26th seat is sold. That is a TOCTOU race, and no amount of caching
or retrying fixes it — the check and the increment must be ONE operation, decided by the
store. (This is what `SELECT … FOR UPDATE` does in the systems we studied.)
"""
from __future__ import annotations

from typing import Any, Protocol, runtime_checkable


@runtime_checkable
class DataStore(Protocol):
    driver: str

    async def get(self, collection: str, key: str) -> dict[str, Any] | None: ...

    async def put(self, collection: str, key: str, value: dict[str, Any]) -> None: ...

    async def delete(self, collection: str, key: str) -> bool: ...

    async def list(self, collection: str, limit: int = 100) -> list[dict[str, Any]]: ...

    async def incr_if_below(self, collection: str, key: str, delta: int,
                            limit: int | None) -> tuple[bool, int]:
        """Atomically add `delta` iff the result stays within `limit`.

        Returns (applied, value_after). `limit=None` means unlimited (always applies).
        A negative `delta` (a release/refund) always applies, clamped at 0 — giving a unit
        back must never be refused because the limit was since lowered.
        """
        ...

    async def health(self) -> bool: ...

    async def close(self) -> None: ...
