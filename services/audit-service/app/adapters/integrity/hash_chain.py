"""Hash-chain integrity — sha256(prev_hash || canonical(event)). Tamper-evident."""
from __future__ import annotations

import hashlib
from typing import Any

from app.adapters.integrity.common import canonical


class HashChainIntegrity:
    driver = "hash-chain"

    def seal(self, prev_hash: str, event: dict[str, Any]) -> str:
        return hashlib.sha256((prev_hash + canonical(event)).encode()).hexdigest()
