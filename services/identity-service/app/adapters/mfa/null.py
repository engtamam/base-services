"""Null MFA — disabled (default). Enrollment refused; verification denies."""
from __future__ import annotations

from app.ports.errors import ConfigError


class NullMfa:
    driver = "null"
    enabled = False

    def provision(self, account: str) -> dict[str, str]:
        raise ConfigError("MFA is disabled (set MFA_DRIVER=totp to enable)")

    def verify(self, secret: str, code: str) -> bool:
        return False
