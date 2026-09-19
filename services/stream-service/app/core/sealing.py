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


def source_aad(source_id: str) -> str:
    """The associated-data that binds a sealed source URL to its own source row.

    SEAL_KEY is one key for the whole service, so an AEAD seal with no AAD authenticates the
    ciphertext but NOT where it lives: a DB tamperer (no key) could copy source X's `url_sealed`
    into source Y's row and it would still decrypt, so Y streams from X's camera credentials. The
    AAD binds the blob to its source id, so a relocated blob fails the GCM tag loudly.

    Bound to the source id, NOT the tenant: the id is unique and PRESERVED verbatim across
    backup/restore — including a restore-as-clone that remaps the tenant but keeps the row id — so
    the sealed URL keeps opening after a legitimate restore while still failing on a cross-row move.
    """
    return f"stream-source|{source_id}"


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

    def seal(self, plaintext: str, aad: str = "") -> str:
        # aad BINDS this ciphertext to its slot (a source id — see source_aad). Without it the
        # single SEAL_KEY decrypts a blob wherever it is stored, so a tampered row could relocate one
        # source's sealed URL onto another. unseal must pass the SAME aad or the tag fails.
        from cryptography.hazmat.primitives.ciphers.aead import AESGCM

        nonce = os.urandom(12)
        ad = aad.encode() if aad else None
        ct = AESGCM(self._key).encrypt(nonce, plaintext.encode(), ad)
        return base64.urlsafe_b64encode(nonce + ct).decode()

    def unseal(self, sealed: str, aad: str = "") -> str:
        from cryptography.hazmat.primitives.ciphers.aead import AESGCM

        raw = base64.urlsafe_b64decode(sealed.encode())
        nonce, ct = raw[:12], raw[12:]
        ad = aad.encode() if aad else None
        return AESGCM(self._key).decrypt(nonce, ct, ad).decode()


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
