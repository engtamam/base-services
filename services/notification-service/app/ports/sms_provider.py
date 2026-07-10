"""SmsProvider port — send one SMS. Returns True on success, False on failure."""
from __future__ import annotations

from typing import Protocol, runtime_checkable


@runtime_checkable
class SmsProvider(Protocol):
    driver: str

    def send(self, to: str, message: str) -> bool: ...
