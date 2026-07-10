"""MfaProvider port — multi-factor auth (default: disabled)."""
from __future__ import annotations

from typing import Protocol, runtime_checkable


@runtime_checkable
class MfaProvider(Protocol):
    driver: str
    enabled: bool

    def provision(self, account: str) -> dict[str, str]:
        """Return {secret, uri} to enroll a new factor (e.g. a TOTP QR URI)."""
        ...

    def verify(self, secret: str, code: str) -> bool:
        """Constant-time verify of a one-time code against the secret."""
        ...
