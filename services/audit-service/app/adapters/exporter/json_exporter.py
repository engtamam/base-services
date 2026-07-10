"""JSON exporter."""
from __future__ import annotations

import json
from typing import Any


class JsonExporter:
    driver = "json"

    def export(self, events: list[dict[str, Any]]) -> tuple[str, bytes]:
        return "application/json", json.dumps(events, ensure_ascii=False, default=str).encode()
