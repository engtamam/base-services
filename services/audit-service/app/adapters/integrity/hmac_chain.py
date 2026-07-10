"""HMAC-chain integrity — keyed hmac-sha256; a verifier needs the key too."""
from __future__ import annotations

import hashlib
import hmac
from typing import Any

from app.adapters.integrity.common import canonical
from app.ports.errors import ConfigError


class HmacChainIntegrity:
    driver = "hmac-chain"

    def __init__(self, key: str) -> None:
        if not key or len(key) < 16:
            raise ConfigError("INTEGRITY_HMAC_KEY must be set (>= 16 chars) for hmac-chain")
        self._key = key.encode()

    def seal(self, prev_hash: str, event: dict[str, Any]) -> str:
        return hmac.new(self._key, (prev_hash + canonical(event)).encode(), hashlib.sha256).hexdigest()
