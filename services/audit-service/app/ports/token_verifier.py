"""TokenVerifier port — verify bearer tokens and return claims.

Default adapters pin the algorithm and check issuer + audience to defeat
algorithm-confusion (CWE-347) and token-misuse across services.
"""
from __future__ import annotations

from typing import Any, Protocol, runtime_checkable


@runtime_checkable
class TokenVerifier(Protocol):
    driver: str

    def verify(self, token: str) -> dict[str, Any]:
        """Return the validated claims, or raise InvalidToken."""
        ...
