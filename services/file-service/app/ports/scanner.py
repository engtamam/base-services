"""Scanner port — reject malicious / disallowed uploads (CWE-434 defense)."""
from __future__ import annotations

from typing import Protocol, runtime_checkable

from app.ports.errors import PortError


class Rejected(PortError):
    """The upload was rejected (malware, disallowed/mismatched type)."""


@runtime_checkable
class Scanner(Protocol):
    driver: str

    def scan(self, data: bytes, content_type: str) -> None:
        """Return None if clean; raise Rejected otherwise."""
        ...
