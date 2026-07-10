"""Stdout Telemetry — wraps the Phase 1 JSON logger + Prometheus counters."""
from __future__ import annotations

from typing import Any

from prometheus_client import Counter

from app.observability.logging import get_logger


class StdoutTelemetry:
    driver = "stdout"

    def __init__(self) -> None:
        self._logger = get_logger("app.telemetry")
        self._counters: dict[str, Counter] = {}

    def event(self, name: str, **fields: Any) -> None:
        # The JSON formatter redacts sensitive keys before emitting.
        self._logger.info(name, extra=fields)

    def counter(self, name: str, value: int = 1, **labels: str) -> None:
        counter = self._counters.get(name)
        if counter is None:
            # Sanitize to a valid Prometheus metric name.
            safe = "".join(c if c.isalnum() else "_" for c in name)
            counter = Counter(safe, f"counter for {name}")
            self._counters[name] = counter
        counter.inc(value)
