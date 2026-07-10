"""TokenIssuer port — SIGN access/refresh tokens (identity is the issuer).

Other services only VERIFY (TokenVerifier port) — they never hold the signing key.
"""
from __future__ import annotations

from typing import Any, Protocol, runtime_checkable


@runtime_checkable
class TokenIssuer(Protocol):
    driver: str

    def issue_access(self, subject: str, claims: dict[str, Any], ttl_seconds: int) -> str:
        """Signed short-lived access token (carries caps/tenant, type=access)."""
        ...

    def issue_refresh(self, subject: str, jti: str, ttl_seconds: int) -> str:
        """Signed long-lived refresh token (carries jti, type=refresh)."""
        ...
