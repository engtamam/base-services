"""NDJSON exporter — one JSON object per line (stream-friendly / SIEM)."""
from __future__ import annotations

import json
from typing import Any


class NdjsonExporter:
    driver = "ndjson"

    def export(self, events: list[dict[str, Any]]) -> tuple[str, bytes]:
        lines = "\n".join(json.dumps(e, ensure_ascii=False, default=str) for e in events)
        return "application/x-ndjson", lines.encode()
