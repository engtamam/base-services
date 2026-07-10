"""Clock port — now() in epoch seconds. Injected so cron/lease logic is testable."""
from __future__ import annotations

from typing import Protocol, runtime_checkable


@runtime_checkable
class Clock(Protocol):
    driver: str

    def now(self) -> float:
        ...
