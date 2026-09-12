"""CSV exporter — flat columns; metadata rendered as a JSON string cell."""
from __future__ import annotations

import csv
import io
import json
from typing import Any

_COLUMNS = ["seq", "ts", "service", "actor", "action", "resource",
            "tenant", "ip", "hash", "prev_hash", "metadata"]

# Spreadsheet formula-injection (CWE-1236): a cell whose text begins with one of these is evaluated
# as a formula by Excel/Sheets when the operator opens the export. Audit fields (service/action/
# resource/tenant/metadata) are caller-supplied, so any leading trigger is neutralised by prefixing
# a single quote — the standard, lossless defence (the value still reads correctly in the cell).
_FORMULA_TRIGGERS = ("=", "+", "-", "@", "\t", "\r")


def _sanitize(value: Any) -> Any:
    if isinstance(value, str) and value and value[0] in _FORMULA_TRIGGERS:
        return "'" + value
    return value


class CsvExporter:
    driver = "csv"

    def export(self, events: list[dict[str, Any]]) -> tuple[str, bytes]:
        buf = io.StringIO()
        writer = csv.DictWriter(buf, fieldnames=_COLUMNS, extrasaction="ignore")
        writer.writeheader()
        for e in events:
            row = {k: _sanitize(e.get(k, "")) for k in _COLUMNS}
            row["metadata"] = _sanitize(json.dumps(e.get("metadata") or {}, ensure_ascii=False))
            writer.writerow(row)
        return "text/csv", buf.getvalue().encode()
