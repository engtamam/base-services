"""Null warehouse — exports nothing, and returns None rather than pretending it did."""
from __future__ import annotations

from typing import Any


class NullWarehouse:
    driver = "null"

    def export(self, rows: list[dict[str, Any]]) -> tuple[str, bytes] | None:
        return None
