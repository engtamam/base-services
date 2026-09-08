"""L1 — WhatsApp Flow crypto. GENERIC: any dynamic (data_exchange) Flow uses this, not just booking.

Meta's contract (verified 2026-09-08). A request carries three base64 fields:
  - `encrypted_aes_key`   — a 128-bit AES key, RSA-OAEP-encrypted for the shop's public key.
  - `initial_vector`      — the AES-GCM IV.
  - `encrypted_flow_data` — the JSON payload, AES-GCM-encrypted, the 128-bit auth tag appended.
RSA padding is OAEP with SHA-256 (MGF1 also SHA-256). The reply is AES-GCM with the SAME key and the
IV **bit-inverted** (each byte XOR 0xFF) — Meta rejects a reply encrypted with the un-flipped IV, and
returns HTTP 421 when it cannot decrypt (its signal to refresh the public key).

This module holds no keys and no state: the PEM is passed in, resolved by a caller that keeps the
plaintext private key out of the request layer. Pure functions, so it is trivially reusable and
testable for ANY dynamic Flow a platform builds on top of it.
"""
from __future__ import annotations

import base64
import json
from typing import Any

from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding
from cryptography.hazmat.primitives.ciphers.aead import AESGCM


class FlowCryptoError(Exception):
    """Decryption/verification failed — the route answers 421 so the client refreshes its key, and
    never leaks which step failed."""


def _flip_iv(iv: bytes) -> bytes:
    """The reply IV is the request IV with every bit inverted — Meta's contract."""
    return bytes(b ^ 0xFF for b in iv)


def decrypt_request(*, encrypted_flow_data: str, encrypted_aes_key: str, initial_vector: str,
                    private_key_pem: bytes, passphrase: bytes | None = None
                    ) -> tuple[dict[str, Any], bytes, bytes]:
    """Return (payload, aes_key, iv). Raises FlowCryptoError on any failure (one opaque error)."""
    try:
        private_key = serialization.load_pem_private_key(private_key_pem, password=passphrase)
        aes_key = private_key.decrypt(
            base64.b64decode(encrypted_aes_key),
            padding.OAEP(mgf=padding.MGF1(algorithm=hashes.SHA256()),
                         algorithm=hashes.SHA256(), label=None))
        iv = base64.b64decode(initial_vector)
        ct = base64.b64decode(encrypted_flow_data)          # ciphertext WITH the 16-byte tag appended
        payload = json.loads(AESGCM(aes_key).decrypt(iv, ct, None).decode("utf-8"))
        if not isinstance(payload, dict):
            raise FlowCryptoError("payload is not an object")
        return payload, aes_key, iv
    except FlowCryptoError:
        raise
    except Exception as exc:   # noqa: BLE001 — never reveal which crypto step failed
        raise FlowCryptoError("flow request could not be decrypted") from exc


def encrypt_response(response: dict[str, Any], *, aes_key: bytes, iv: bytes) -> str:
    """AES-GCM with the request key and the FLIPPED IV, base64. Tag is appended by AESGCM."""
    data = json.dumps(response, separators=(",", ":")).encode("utf-8")
    return base64.b64encode(AESGCM(aes_key).encrypt(_flip_iv(iv), data, None)).decode("ascii")
