"""Null token issuer — deny-by-default. Boots with zero config; issues nothing
until a real driver (hs256/rs256) + signing key is configured."""
from __future__ import annotations

from typing import Any

from app.ports.errors import ConfigError


class NullIssuer:
    driver = "null"

    def issue_access(self, subject: str, claims: dict[str, Any], ttl_seconds: int) -> str:
        raise ConfigError("no token issuer configured (set TOKEN_ISSUER_DRIVER + key)")

    def issue_refresh(self, subject: str, jti: str, ttl_seconds: int) -> str:
        raise ConfigError("no token issuer configured (set TOKEN_ISSUER_DRIVER + key)")
