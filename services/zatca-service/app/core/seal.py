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


def seal(plaintext: str, seal_key: str) -> str:
    if plaintext is None:
        return ""
    nonce = os.urandom(12)
    ct = AESGCM(_key(seal_key)).encrypt(nonce, plaintext.encode(), None)
    return base64.b64encode(nonce + ct).decode()


def unseal(token: str, seal_key: str) -> str | None:
    if not token:
        return None
    try:
        raw = base64.b64decode(token)
        return AESGCM(_key(seal_key)).decrypt(raw[:12], raw[12:], None).decode()
    except Exception:
        return None
