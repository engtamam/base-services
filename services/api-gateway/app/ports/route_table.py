"""RouteTable port — resolve an inbound path to a Route (config-driven)."""
from __future__ import annotations

from typing import TYPE_CHECKING, Protocol, runtime_checkable

if TYPE_CHECKING:
    from app.core.models import Route


@runtime_checkable
class RouteTable(Protocol):
    driver: str

    def match(self, path: str) -> "Route | None":
        """Return the longest-prefix Route for a path, or None."""
        ...

    def all(self) -> "list[Route]":
        ...
