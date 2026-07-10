"""Argon2 password hasher — memory-hard, modern default (CWE-916/327-safe)."""
from __future__ import annotations

from argon2 import PasswordHasher as _Argon2
from argon2.exceptions import VerifyMismatchError, VerificationError, InvalidHashError


class Argon2Hasher:
    driver = "argon2"

    def __init__(self) -> None:
        self._ph = _Argon2()

    def hash(self, password: str) -> str:
        return self._ph.hash(password)

    def verify(self, password: str, hashed: str) -> bool:
        try:
            return self._ph.verify(hashed, password)
        except (VerifyMismatchError, VerificationError, InvalidHashError, Exception):
            return False

    def needs_rehash(self, hashed: str) -> bool:
        try:
            return self._ph.check_needs_rehash(hashed)
        except Exception:
            return True
