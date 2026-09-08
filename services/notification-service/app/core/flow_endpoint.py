"""WhatsApp Flow DATA ENDPOINT — the encrypted server callback that drives a Flow's screens.

See docs/design/whatsapp-booking-flow.md. This is the transport + envelope layer (phase 1): it
decrypts a Flow request, dispatches on `action`, and encrypts the reply. The booking state machine
(phase 2+) plugs in as the `handler` — this module authors no screens itself.

CRYPTO (Meta's contract, verified 2026-09-08). A request carries three base64 fields:
  - `encrypted_aes_key`   — a 128-bit AES key, RSA-OAEP-encrypted for THIS tenant's public key.
  - `initial_vector`      — the AES-GCM IV.
  - `encrypted_flow_data` — the JSON payload, AES-GCM-encrypted, the 128-bit auth tag appended.
RSA padding is OAEP with SHA-256 (MGF1 also SHA-256). The reply is AES-GCM with the SAME key and the
IV **bit-inverted** (each byte XOR 0xFF) — Meta rejects a reply encrypted with the un-flipped IV.

Keys are PER TENANT (per WABA): the private key is sealed in the secret store, resolved by a caller
that never lets plaintext reach a route. This module takes the PEM in and holds nothing.
"""
from __future__ import annotations

import base64
import json
from typing import Any, Awaitable, Callable

from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

PING = "ping"
INIT = "INIT"
BACK = "BACK"
DATA_EXCHANGE = "data_exchange"


class FlowCryptoError(Exception):
    """Decryption/verification failed. The route answers Meta's required 421 so the client refreshes
    its public key, and never leaks which step failed."""


# handler(payload) -> response dict. Injected so this module stays domain-free (no booking here).
Handler = Callable[[dict[str, Any]], Awaitable[dict[str, Any]]]


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
        plaintext = AESGCM(aes_key).decrypt(iv, ct, None)
        payload = json.loads(plaintext.decode("utf-8"))
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
    ct = AESGCM(aes_key).encrypt(_flip_iv(iv), data, None)
    return base64.b64encode(ct).decode("ascii")


async def process(*, encrypted_flow_data: str, encrypted_aes_key: str, initial_vector: str,
                  private_key_pem: bytes, handler: Handler,
                  passphrase: bytes | None = None) -> str:
    """Decrypt → dispatch → encrypt. Returns the base64 reply body Meta expects.

    `ping` is answered here (a health check must never depend on the booking handler); every other
    action is delegated to `handler`, which returns a bare navigation/terminal dict ({screen, data}
    or {data:{...}}). Errors from the handler are the caller's to shape into an error_message screen.
    """
    payload, aes_key, iv = decrypt_request(
        encrypted_flow_data=encrypted_flow_data, encrypted_aes_key=encrypted_aes_key,
        initial_vector=initial_vector, private_key_pem=private_key_pem, passphrase=passphrase)

    if payload.get("action") == PING:
        response: dict[str, Any] = {"data": {"status": "active"}}
    else:
        response = await handler(payload)
    return encrypt_response(response, aes_key=aes_key, iv=iv)
