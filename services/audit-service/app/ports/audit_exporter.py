"""AuditExporter port — serialize audit events for export."""
from __future__ import annotations

from typing import Any, Protocol, runtime_checkable


@runtime_checkable
class AuditExporter(Protocol):
    driver: str

    def export(self, events: list[dict[str, Any]]) -> tuple[str, bytes]:
        """Return (content_type, body)."""
        ...
