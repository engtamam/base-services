"""CSV warehouse export — stdlib csv, stable column order, injection-safe cells.

A leading `=`, `+`, `-` or `@` in a cell makes Excel/Sheets execute it as a formula
(CSV injection, CWE-1236). Every such cell is prefixed with a single quote.
"""
from __future__ import annotations

import csv
import io
from typing import Any

_DANGEROUS = ("=", "+", "-", "@", "\t", "\r")


def _safe(value: Any) -> Any:
    s = "" if value is None else str(value)
    return f"'{s}" if s[:1] in _DANGEROUS else value


class CsvWarehouse:
    driver = "csv"

    def export(self, rows: list[dict[str, Any]]) -> tuple[str, bytes] | None:
        if not rows:
            return "text/csv", b""
        columns: list[str] = []
        for row in rows:
            for key in row:
                if key not in columns:
                    columns.append(key)
        buf = io.StringIO()
        writer = csv.DictWriter(buf, fieldnames=columns, extrasaction="ignore")
        writer.writeheader()
        for row in rows:
            writer.writerow({c: _safe(row.get(c)) for c in columns})
        return "text/csv", buf.getvalue().encode("utf-8")
