"""AES-256-GCM seal/unseal for CSID material at rest (uses cryptography, already present).

Key = SHA-256(config seal key) → 32 bytes. Token = base64( nonce(12) || ciphertext||tag ).
"""
from __future__ import annotations

import base64
import hashlib
import os

from cryptography.hazmat.primitives.ciphers.aead import AESGCM


def _key(seal_key: str) -> bytes:
    return hashlib.sha256((seal_key or "").encode()).digest()


def seal(plaintext: str, seal_key: str, aad: str = "") -> str:
    # aad BINDS this ciphertext to its slot (collection|tenant|field — see stores.py). The seal key
    # is one per service, so without it a DB tamperer (no key) could copy tenant A's sealed CSID cert
    # into tenant B's row, or swap the cert blob into the private-key field, and it would still
    # decrypt — B then signing as A. A relocated/renamed blob now fails the GCM tag loudly.
    if plaintext is None:
        return ""
    nonce = os.urandom(12)
    ad = aad.encode() if aad else None
    ct = AESGCM(_key(seal_key)).encrypt(nonce, plaintext.encode(), ad)
    return base64.b64encode(nonce + ct).decode()


def unseal(token: str, seal_key: str, aad: str = "") -> str | None:
    if not token:
        return None
    try:
        raw = base64.b64decode(token)
        ad = aad.encode() if aad else None
        return AESGCM(_key(seal_key)).decrypt(raw[:12], raw[12:], ad).decode()
    except Exception:
        return None
