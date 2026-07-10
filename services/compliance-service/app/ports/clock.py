"""Clock port — now() in epoch seconds (UTC). Injected so SLA/breach clocks are testable."""
from __future__ import annotations

from typing import Protocol, runtime_checkable


@runtime_checkable
class Clock(Protocol):
    driver: str

    def now(self) -> float:
        ...
