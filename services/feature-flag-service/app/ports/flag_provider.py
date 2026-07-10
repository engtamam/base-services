"""FlagProvider port — where flag definitions come from.

`local` owns them in our DataStore; `unleash`/`launchdarkly` delegate to an external
system; `null` returns nothing (every flag falls back to its default = OFF, fail-safe).
The evaluation engine is OURS in every case — providers only supply definitions.
"""
from __future__ import annotations

from typing import Any, Protocol, runtime_checkable

from app.ports.errors import PortError


class ProviderUnavailable(PortError):
    """Provider unreachable — callers fall back to the flag default (fail-safe)."""


@runtime_checkable
class FlagProvider(Protocol):
    driver: str

    async def get(self, key: str) -> dict[str, Any] | None:
        """Return the flag definition, or None if unknown."""
        ...

    async def list(self, limit: int = 500) -> list[dict[str, Any]]:
        ...
