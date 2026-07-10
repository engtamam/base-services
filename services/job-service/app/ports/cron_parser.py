"""CronParser port — next fire time for a cron expression, in UTC epoch seconds."""
from __future__ import annotations

from typing import Protocol, runtime_checkable


@runtime_checkable
class CronParser(Protocol):
    driver: str

    def next_after(self, expr: str, after: float) -> float:
        ...

    def validate(self, expr: str) -> None:
        ...
