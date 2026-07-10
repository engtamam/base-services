"""EmailProvider port — send one email. Returns True on success, False on failure."""
from __future__ import annotations

from typing import Protocol, runtime_checkable


@runtime_checkable
class EmailProvider(Protocol):
    driver: str

    def send(self, to: str, subject: str, html: str, text: str, sender: str) -> bool: ...
