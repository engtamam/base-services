"""username identifier — the historical default. Normalization is exactly today's behavior.

The uniqueness index has always lowercased the username, so `normalize` is `.lower()`: selecting
this adapter reproduces the pre-port behavior byte for byte. Nothing breaks for a product that
never asked for email.
"""
from __future__ import annotations

from app.ports.errors import ValidationError

_MAX = 128


class UsernameIdentifier:
    driver = "username"

    def validate(self, raw: str) -> None:
        v = (raw or "").strip()
        if not v:
            raise ValidationError("username must not be empty")
        if v != raw:
            raise ValidationError("username must not have leading or trailing whitespace")
        if len(v) > _MAX:
            raise ValidationError(f"username must be at most {_MAX} characters")

    def normalize(self, raw: str) -> str:
        return raw.lower()          # what the index has always done — case-insensitive uniqueness

    def display(self, raw: str) -> str:
        return raw
