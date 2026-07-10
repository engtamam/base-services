"""Null TokenVerifier — DENY-BY-DEFAULT. Zero config.

The default when no auth is configured: every token is rejected. A service boots
with zero config and stays closed until an operator selects a real driver
(hs256/rs256/jwks) and supplies a key. Never grants access.
"""
from __future__ import annotations

from typing import Any

from app.ports.errors import InvalidToken


class NullVerifier:
    driver = "null"

    def verify(self, token: str) -> dict[str, Any]:
        raise InvalidToken("no token verifier configured (deny-by-default)")
