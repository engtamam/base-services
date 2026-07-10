"""email identifier — validates, and folds case so one address is one account.

The validation is a tight, dependency-free check: exactly one `@`, non-empty local and domain,
a dot in the domain, no whitespace. It is intentionally conservative (it rejects some technically
legal but exotic addresses) rather than permissive — a login identifier that looks like an email
but is not is worse than a rare false rejection at signup.

Normalization lowercases the whole address. The local part is technically case-sensitive per RFC,
but in practice no provider treats `Alice@x` and `alice@x` as different mailboxes, and treating
them as ONE identity is the safe, expected behavior — it prevents duplicate accounts.
"""
from __future__ import annotations

import re

from app.ports.errors import ValidationError

# one @, no whitespace, a dotted domain. Deliberately conservative.
_EMAIL = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
_MAX = 254          # RFC 5321 maximum total length


class EmailIdentifier:
    driver = "email"

    def validate(self, raw: str) -> None:
        v = (raw or "").strip()
        if not v:
            raise ValidationError("email must not be empty")
        if len(v) > _MAX:
            raise ValidationError(f"email must be at most {_MAX} characters")
        if not _EMAIL.match(v):
            raise ValidationError(f"not a valid email address: {raw!r}")

    def normalize(self, raw: str) -> str:
        return raw.strip().lower()

    def display(self, raw: str) -> str:
        return raw.strip()
