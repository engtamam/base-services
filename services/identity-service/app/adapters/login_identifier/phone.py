"""phone identifier — normalizes to E.164 so ONE human is ONE account across spellings.

`0501234567`, `+966501234567` and `966501234567` are the same person. `normalize` folds them all to
the canonical E.164 (`+966501234567`), and because uniqueness AND lookup both key on that (see the
LoginIdentifier port and user_repository), the three spellings can never become three accounts — the
reporter's first concern. A NATIONAL number (leading 0, or bare digits with no country code) is
resolved with the default country calling code, which is CONFIG (IDENTITY_PHONE_DEFAULT_CC) so this
works for any market, not a hardcoded +966.

Dependency-free on purpose (minimal attack surface): a tight E.164 canonicaliser, deliberately
conservative — a value that cannot be made a well-formed E.164 number is rejected at signup rather
than stored as an identity that half-matches later.
"""
from __future__ import annotations

import re

from app.ports.errors import ValidationError

_DIGITS = re.compile(r"\D+")          # anything not a digit — spaces, dashes, parens, dots
_E164 = re.compile(r"^\+[1-9]\d{7,14}$")   # + then 8..15 digits, first digit non-zero (ITU E.164)


class PhoneIdentifier:
    driver = "phone"

    def __init__(self, default_cc: str = "966") -> None:
        # The country calling code for a NATIONAL number (no + / no country code). Digits only.
        self._cc = _DIGITS.sub("", str(default_cc or "966")) or "966"

    def _canonical(self, raw: str) -> str:
        """Best-effort E.164. Never raises — validate() is the gate; this must be idempotent so it can
        key an already-normalized value at lookup time."""
        v = (raw or "").strip()
        if not v:
            return ""
        plus = v.startswith("+")
        digits = _DIGITS.sub("", v)
        if not digits:
            return ""
        if plus:
            return "+" + digits                      # already international
        if digits.startswith("00"):
            return "+" + digits[2:]                  # 00 = international access prefix
        if digits.startswith("0"):
            return "+" + self._cc + digits[1:]       # national: leading 0 -> default country code
        if digits.startswith(self._cc):
            return "+" + digits                      # bare digits that already include the country code
        # A bare national number with no leading 0 and not obviously prefixed: assume it needs the CC.
        return "+" + self._cc + digits

    def validate(self, raw: str) -> None:
        if not (raw or "").strip():
            raise ValidationError("phone must not be empty")
        if not _E164.match(self._canonical(raw)):
            raise ValidationError(f"not a valid phone number: {raw!r}")

    def normalize(self, raw: str) -> str:
        return self._canonical(raw)                  # the E.164 key — uniqueness AND lookup

    def display(self, raw: str) -> str:
        return self._canonical(raw)                  # E.164 is the readable, unambiguous form
