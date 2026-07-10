"""CSV exporter — flat columns; metadata rendered as a JSON string cell."""
from __future__ import annotations

import csv
import io
import json
from typing import Any

_COLUMNS = ["seq", "ts", "service", "actor", "action", "resource",
            "tenant", "ip", "hash", "prev_hash", "metadata"]


class CsvExporter:
    driver = "csv"

    def export(self, events: list[dict[str, Any]]) -> tuple[str, bytes]:
        buf = io.StringIO()
        writer = csv.DictWriter(buf, fieldnames=_COLUMNS, extrasaction="ignore")
        writer.writeheader()
        for e in events:
            row = {k: e.get(k, "") for k in _COLUMNS}
            row["metadata"] = json.dumps(e.get("metadata") or {}, ensure_ascii=False)
            writer.writerow(row)
        return "text/csv", buf.getvalue().encode()
