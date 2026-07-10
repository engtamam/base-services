"""Null signer — signs nothing and verifies nothing. Fail-closed: verify always False."""
from __future__ import annotations


class NullSigner:
    driver = "null"

    def sign(self, *, body: bytes, secret: str, timestamp: int) -> str:
        return ""

    def verify(self, *, body: bytes, header: str, secrets: list[str], timestamp: int,
               tolerance_seconds: int) -> bool:
        return False            # never accept an unverified inbound webhook
