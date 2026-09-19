"""AES-256-GCM seal/unseal for third-party credentials at rest.

Access and refresh tokens are bearer credentials for someone else's system — a database
dump must not hand an attacker the customer's Salesforce. Key = SHA-256(seal key).
Token = base64(nonce(12) || ciphertext||tag).
"""
from __future__ import annotations

import base64
import hashlib
import os

from cryptography.hazmat.primitives.ciphers.aead import AESGCM


def _key(seal_key: str) -> bytes:
    return hashlib.sha256((seal_key or "").encode()).digest()


def token_aad(connection_id: str, field: str) -> str:
    """Bind a connection's sealed token to its exact (connection, field) slot. The seal key is one
    per service, so without this a DB tamperer (no key) could copy connection A's access token into
    connection B's row — B then calling the provider as A. A relocated/renamed blob now fails the
    GCM tag instead."""
    return f"connection|{connection_id}|{field}"


def spec_secret_aad(spec_id: str) -> str:
    """Bind a spec's sealed OAuth client secret to its spec id, so it cannot be relocated onto
    another spec's row and reused."""
    return f"spec|{spec_id}|client_secret"


def seal(plaintext: str, seal_key: str, aad: str = "") -> str:
    if not plaintext:
        return ""
    nonce = os.urandom(12)
    # aad binds this ciphertext to its slot (see token_aad/spec_secret_aad); empty => unbound, kept
    # only so a caller with no natural identity still round-trips. A bound seal must be unsealed with
    # the same aad or the GCM tag fails.
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


def redact(text: str, *secrets: str) -> str:
    """Never let a credential reach a log line or an error message."""
    out = text or ""
    for s in secrets:
        if s and len(s) >= 6:
            out = out.replace(s, "***redacted***")
    return out
