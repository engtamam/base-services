"""SignedUrlProvider port — time-boxed, tamper-proof access tokens."""
from __future__ import annotations

from typing import Any, Protocol, runtime_checkable


@runtime_checkable
class SignedUrlProvider(Protocol):
    driver: str

    def sign(self, key: str, extra: dict[str, Any] | None = None) -> str:
        """Return an opaque token granting access to `key` until it expires."""
        ...

    def verify(self, token: str) -> dict[str, Any] | None:
        """Return the payload ({key, ...}) if valid & unexpired, else None."""
        ...
