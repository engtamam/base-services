"""Shared helpers for payment adapters."""
from __future__ import annotations

import hashlib
import hmac

from app.core.currency import major as _major
from app.core.currency import to_minor as _to_minor


def major(amount_minor: int, currency: str = "SAR") -> str:
    """Minor units -> major string with the currency's real precision (KWD/BHD = 3-dp)."""
    return _major(amount_minor, currency)


def to_minor(amount_major, currency: str = "SAR") -> int:
    """Major units -> integer minor units at the currency's real precision (inverse of major())."""
    return _to_minor(amount_major, currency)


def hmac_sha256_hex(secret: str, body: bytes) -> str:
    return hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()


def constant_eq(a: str, b: str) -> bool:
    return hmac.compare_digest(a or "", b or "")
