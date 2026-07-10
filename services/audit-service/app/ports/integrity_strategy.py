"""IntegrityStrategy port — seal an audit event into a tamper-evident chain."""
from __future__ import annotations

from typing import Any, Protocol, runtime_checkable


@runtime_checkable
class IntegrityStrategy(Protocol):
    driver: str

    def seal(self, prev_hash: str, event: dict[str, Any]) -> str:
        """Return this event's hash, chained from prev_hash (deterministic)."""
        ...
