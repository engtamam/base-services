"""LoginIdentifier port — how a login identifier is validated, normalized, and keyed.

What varies between products is not "username vs email" — it is how the identifier is checked,
folded to a canonical form, and shown back. That is the seam, so it is a port with adapters
selected by config (IDENTITY_LOGIN_FIELD), exactly like PasswordHasher / TokenIssuer / MfaProvider.

Adding phone (E.164) or a federated provider later is a new adapter, not a change to AuthService.

`normalize` is the load-bearing method: uniqueness and lookup are BOTH done on the normalized
key, so two spellings of the same identity (Alice@x vs alice@x) can never become two accounts.
"""
from __future__ import annotations

from typing import Protocol, runtime_checkable

from app.ports.errors import ValidationError  # noqa: F401 — re-exported for adapters/callers


@runtime_checkable
class LoginIdentifier(Protocol):
    driver: str

    def validate(self, raw: str) -> None:
        """Raise ValidationError if `raw` is not a well-formed identifier of this kind."""
        ...

    def normalize(self, raw: str) -> str:
        """The canonical key used for uniqueness AND lookup. Must be idempotent and stable."""
        ...

    def display(self, raw: str) -> str:
        """The form shown to a human. Often the original, trimmed."""
        ...
