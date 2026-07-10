"""PasswordHasher port — hash & verify passwords (never store plaintext)."""
from __future__ import annotations

from typing import Protocol, runtime_checkable


@runtime_checkable
class PasswordHasher(Protocol):
    driver: str

    def hash(self, password: str) -> str: ...

    def verify(self, password: str, hashed: str) -> bool:
        """Constant-time verify; returns False on any mismatch/format error."""
        ...

    def needs_rehash(self, hashed: str) -> bool: ...
