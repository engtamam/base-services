"""Telemetry port — structured events + counters (logs/metrics/traces)."""
from __future__ import annotations

from typing import Any, Protocol, runtime_checkable


@runtime_checkable
class Telemetry(Protocol):
    driver: str

    def event(self, name: str, **fields: Any) -> None:
        """Emit a structured event (secrets are redacted downstream)."""
        ...

    def counter(self, name: str, value: int = 1, **labels: str) -> None:
        """Increment a named counter."""
        ...
