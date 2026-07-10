"""Clock port — now() in epoch seconds (UTC). Injected for deterministic bucket/lateness tests."""
from __future__ import annotations

from typing import Protocol, runtime_checkable


@runtime_checkable
class Clock(Protocol):
    driver: str

    def now(self) -> float:
        ...
