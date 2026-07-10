"""bcrypt password hasher — infra-gated (needs the [bcrypt] extra)."""
from __future__ import annotations

from app.adapters.lazy import load


class BcryptHasher:
    driver = "bcrypt"

    def __init__(self) -> None:
        self._bcrypt = load("bcrypt", "bcrypt")

    def hash(self, password: str) -> str:
        return self._bcrypt.hashpw(password.encode(), self._bcrypt.gensalt()).decode()

    def verify(self, password: str, hashed: str) -> bool:
        try:
            return self._bcrypt.checkpw(password.encode(), hashed.encode())
        except Exception:
            return False

    def needs_rehash(self, hashed: str) -> bool:
        return False
