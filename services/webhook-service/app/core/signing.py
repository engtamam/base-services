"""HMAC signing — signs `timestamp.body`, verifies constant-time, supports rotation.

Header format (Stripe-compatible): `t=<unix>,v1=<hex>`.
The timestamp is INSIDE the signed material, so a captured payload cannot be replayed
outside the tolerance window. We sign the exact bytes we send — never a re-serialization.
"""
from __future__ import annotations

import hashlib
import hmac
import time


def signed_payload(timestamp: int, body: bytes) -> bytes:
    return f"{int(timestamp)}.".encode() + body


def compute(secret: str, timestamp: int, body: bytes) -> str:
    return hmac.new(secret.encode(), signed_payload(timestamp, body), hashlib.sha256).hexdigest()


def sign(body: bytes, secret: str, timestamp: int | None = None) -> str:
    ts = int(timestamp if timestamp is not None else time.time())
    return f"t={ts},v1={compute(secret, ts, body)}"


def parse(header: str) -> tuple[int | None, list[str]]:
    ts: int | None = None
    sigs: list[str] = []
    for part in (header or "").split(","):
        k, _, v = part.strip().partition("=")
        if k == "t":
            try:
                ts = int(v)
            except ValueError:
                return None, []
        elif k.startswith("v"):
            sigs.append(v)
    return ts, sigs


def verify(body: bytes, header: str, secrets: list[str], *, now: int | None = None,
           tolerance_seconds: int = 300) -> bool:
    """Constant-time, tolerant of clock skew, accepts ANY current secret (rotation)."""
    ts, sigs = parse(header)
    if ts is None or not sigs or not secrets:
        return False
    current = int(now if now is not None else time.time())
    if abs(current - ts) > tolerance_seconds:
        return False                                    # replay protection
    for secret in secrets:
        if not secret:
            continue
        expected = compute(secret, ts, body)
        for sig in sigs:
            if hmac.compare_digest(expected, sig):      # constant-time
                return True
    return False
