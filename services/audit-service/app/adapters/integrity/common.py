"""Canonical serialization for chaining — deterministic, excludes the hash field."""
from __future__ import annotations

import json
from typing import Any


def canonical(event: dict[str, Any]) -> str:
    # Exclude the derived 'hash' and any internal '_'-prefixed routing fields, so
    # integrity covers exactly the audit content and is stable across storage.
    payload = {k: v for k, v in event.items() if k != "hash" and not k.startswith("_")}
    return json.dumps(payload, sort_keys=True, separators=(",", ":"),
                      ensure_ascii=False, default=str)
