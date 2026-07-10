"""Warehouse port — export rollups/raw for ETL/BI.

`null` exports nothing (and says so). `csv` renders bytes the caller can stream or store.
BigQuery/Snowflake drivers ship WITH their adapter — never a config knob before its code.
"""
from __future__ import annotations

from typing import Any, Protocol, runtime_checkable


@runtime_checkable
class Warehouse(Protocol):
    driver: str

    def export(self, rows: list[dict[str, Any]]) -> tuple[str, bytes] | None:
        """Return (content_type, payload) or None when the driver exports nothing."""
        ...
