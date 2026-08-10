"""composite identifier — accept SEVERAL kinds of login field at once (e.g. phone OR email).

A global product wants email; a Saudi shop wants phone; another wants both — from ONE system. When
IDENTITY_LOGIN_FIELD lists more than one kind, this routes a raw value to the first sub-identifier
that accepts it and normalizes with THAT one.

The canonical key is TYPE-NAMESPACED (`phone:+9665…`, `email:a@b.com`) so a phone and a username that
happen to look alike can never collide into one account — the reporter's "uniqueness must survive
normalization", extended across kinds. A SINGLE configured kind never uses this class (the container
picks the plain adapter), so an existing deployment behaves exactly as before, no prefix, nothing to
migrate.
"""
from __future__ import annotations

from app.ports.errors import ValidationError
from app.ports.login_identifier import LoginIdentifier


class CompositeIdentifier:
    driver = "composite"

    def __init__(self, members: list[tuple[str, LoginIdentifier]]) -> None:
        if not members:
            raise ValueError("CompositeIdentifier needs at least one member")
        self._members = members            # ordered [(name, adapter)]; first match wins

    def _match(self, raw: str) -> tuple[str, LoginIdentifier] | tuple[None, None]:
        for name, m in self._members:
            try:
                m.validate(raw)
                return name, m
            except ValidationError:
                continue
        return None, None

    def validate(self, raw: str) -> None:
        if self._match(raw)[1] is None:
            kinds = ", ".join(n for n, _ in self._members)
            raise ValidationError(f"not a valid login identifier ({kinds}): {raw!r}")

    def normalize(self, raw: str) -> str:
        name, m = self._match(raw)
        if m is None:
            raise ValidationError(f"not a valid login identifier: {raw!r}")
        return f"{name}:{m.normalize(raw)}"        # type-namespaced — no cross-kind collision

    def display(self, raw: str) -> str:
        _, m = self._match(raw)
        return m.display(raw) if m is not None else raw.strip()
