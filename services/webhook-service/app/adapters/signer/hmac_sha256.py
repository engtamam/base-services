"""HMAC-SHA256 signer (Stripe-compatible `t=…,v1=…`) with rotation + replay protection."""
from __future__ import annotations

from app.core import signing


class HmacSha256Signer:
    driver = "hmac-sha256"

    def sign(self, *, body: bytes, secret: str, timestamp: int) -> str:
        return signing.sign(body, secret, timestamp)

    def verify(self, *, body: bytes, header: str, secrets: list[str], timestamp: int,
               tolerance_seconds: int) -> bool:
        return signing.verify(body, header, secrets, now=timestamp,
                              tolerance_seconds=tolerance_seconds)
