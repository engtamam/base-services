"""RateLimiter port — throttle by key (per IP / API key / tenant)."""
from __future__ import annotations

from typing import Protocol, runtime_checkable


@runtime_checkable
class RateLimiter(Protocol):
    driver: str

    async def allow(self, key: str) -> bool:
        """Return True if the action is allowed, False if the key is throttled."""
        ...

    async def reset(self, key: str) -> None: ...

    async def health(self) -> bool: ...

    async def close(self) -> None: ...
