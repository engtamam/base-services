"""No integrity — audit without tamper-evidence (fastest)."""
from __future__ import annotations

from typing import Any


class NoneIntegrity:
    driver = "none"

    def seal(self, prev_hash: str, event: dict[str, Any]) -> str:
        return ""
