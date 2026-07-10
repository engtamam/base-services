"""Signer port — sign the exact bytes we send; verify inbound with a timestamp.

Signing covers `timestamp.body` so a captured payload cannot be replayed later, and
verification is constant-time. Endpoints hold multiple secrets so a rotation can accept
the old secret while already signing with the new one.
"""
from __future__ import annotations

from typing import Protocol, runtime_checkable


@runtime_checkable
class Signer(Protocol):
    driver: str

    def sign(self, *, body: bytes, secret: str, timestamp: int) -> str:
        """Return the signature header value (e.g. `t=1700000000,v1=<hex>`)."""
        ...

    def verify(self, *, body: bytes, header: str, secrets: list[str], timestamp: int,
               tolerance_seconds: int) -> bool:
        """Constant-time verify against ANY of `secrets` (rotation) + timestamp window."""
        ...
