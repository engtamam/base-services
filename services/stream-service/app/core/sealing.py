"""Credential sealing + URL redaction.

A source URL carries `rtsp://user:pass@host/path`. Two rules the whole service depends on:

  1. SEAL at rest — the URL is stored encrypted (AES-256-GCM), so a leaked datastore row is not a
     leaked camera password. The key is `SEAL_KEY` (config, required in production).
  2. REDACT everywhere else — any URL that leaves the service (API response, manifest, event, HLS
     playlist, log line, error message) is reduced to `scheme://host` with credentials stripped.

The plaintext URL exists only transiently, in-process, when an engine is about to open the source.
"""
from __future__ import annotations

import base64
import os
from urllib.parse import urlsplit, urlunsplit


def redact_url(url: str) -> str:
    """`rtsp://user:pass@cam:554/stream` -> `rtsp://cam:554`. Never raises; a weird string returns ''."""
    try:
        parts = urlsplit(url)
    except Exception:   # noqa: BLE001
        return ""
    if not parts.scheme or not parts.hostname:
        return ""
    host = parts.hostname
    if parts.port:
        host = f"{host}:{parts.port}"
    return urlunsplit((parts.scheme, host, "", "", ""))


class Sealer:
    """AES-256-GCM seal/unseal. `SEAL_KEY` may be raw 32 bytes, base64, or any string (hashed to 32)."""

    def __init__(self, key: str) -> None:
        if not key:
            raise ValueError("SEAL_KEY is required to seal source credentials")
        self._key = _derive_key(key)

    def seal(self, plaintext: str) -> str:
        from cryptography.hazmat.primitives.ciphers.aead import AESGCM

        nonce = os.urandom(12)
        ct = AESGCM(self._key).encrypt(nonce, plaintext.encode(), None)
        return base64.urlsafe_b64encode(nonce + ct).decode()

    def unseal(self, sealed: str) -> str:
        from cryptography.hazmat.primitives.ciphers.aead import AESGCM

        raw = base64.urlsafe_b64decode(sealed.encode())
        nonce, ct = raw[:12], raw[12:]
        return AESGCM(self._key).decrypt(nonce, ct, None).decode()


def _derive_key(key: str) -> bytes:
    import hashlib

    # Accept a raw/base64 32-byte key as-is; otherwise derive a stable 32-byte key from the string.
    try:
        raw = base64.urlsafe_b64decode(key)
        if len(raw) == 32:
            return raw
    except Exception:   # noqa: BLE001
        pass
    if len(key.encode()) == 32:
        return key.encode()
    return hashlib.sha256(key.encode()).digest()
