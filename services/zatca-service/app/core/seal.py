"""AES-256-GCM seal/unseal for CSID material at rest (uses cryptography, already present).

Key = SHA-256(config seal key) → 32 bytes. Token = base64( nonce(12) || ciphertext||tag ).
"""
from __future__ import annotations

import base64
import hashlib
import os

from cryptography.hazmat.primitives.ciphers.aead import AESGCM


class CsidSealError(Exception):
    """A sealed CSID blob is PRESENT but could not be decrypted — a wrong/rotated ZATCA_SEAL_KEY or a
    tampered row. Distinct from an ABSENT blob (unseal returns None). Masking this as None made
    load_secrets return None, which finalize/sweep read as 'not onboarded' and silently parked every
    invoice in 'pending' with nobody told the key was wrong (zatca-007). It must surface loudly."""


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
        return None                                 # genuinely ABSENT — not onboarded
    from cryptography.exceptions import InvalidTag
    try:
        raw = base64.b64decode(token)
        ad = aad.encode() if aad else None
        key = _key(seal_key)
        try:
            return AESGCM(key).decrypt(raw[:12], raw[12:], ad).decode()
        except InvalidTag:
            if ad is None:
                raise
            # Backward compatibility: a CSID blob sealed BEFORE AAD binding (associated_data=None)
            # fails the AAD attempt above. Decrypt it in the legacy no-AAD format so upgrading never
            # orphans an existing tenant's cert/key (which would silently halt all signing — the same
            # incident class the fleet AEAD fix closed elsewhere; this file was missed then).
            return AESGCM(key).decrypt(raw[:12], raw[12:], None).decode()  # aad: ok — legacy pre-AAD CSID blob; primary decrypt above binds the AAD
    except Exception as exc:
        # A PRESENT token that will not decrypt is a wrong/rotated key or a tampered row, NOT an absent
        # CSID. Raise loudly (zatca-007) so it surfaces as a 503/alert instead of masquerading as
        # 'not onboarded' and silently parking every invoice in pending.
        raise CsidSealError(f"CSID blob present but undecryptable: {type(exc).__name__}") from exc
