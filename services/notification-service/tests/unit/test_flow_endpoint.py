"""WhatsApp Flow data-endpoint crypto + envelope (phase 1).

We act as Meta's client: generate a keypair, encrypt a request exactly as Meta does (RSA-OAEP-SHA256
for the AES key, AES-GCM for the payload with the tag appended), then assert our endpoint decrypts,
dispatches, and encrypts a reply we can decrypt back with the flipped IV.
"""
from __future__ import annotations

import asyncio
import base64
import json
import os

from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from app.core import flow_endpoint as fe



def _keypair(passphrase: bytes | None = None):
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric import rsa
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    enc = (serialization.BestAvailableEncryption(passphrase) if passphrase
           else serialization.NoEncryption())
    priv = key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, enc)
    pub = key.public_key().public_bytes(serialization.Encoding.PEM,
                                        serialization.PublicFormat.SubjectPublicKeyInfo)
    return priv, pub

def run(c):
    return asyncio.run(c)


def _encrypt_like_meta(payload: dict, public_pem: bytes) -> tuple[str, str, str, bytes, bytes]:
    """Return (encrypted_flow_data, encrypted_aes_key, initial_vector, aes_key, iv) as WhatsApp sends."""
    aes_key = os.urandom(16)                       # 128-bit
    iv = os.urandom(12)
    pub = serialization.load_pem_public_key(public_pem)
    enc_key = pub.encrypt(aes_key, padding.OAEP(mgf=padding.MGF1(hashes.SHA256()),
                                                algorithm=hashes.SHA256(), label=None))
    ct = AESGCM(aes_key).encrypt(iv, json.dumps(payload).encode(), None)
    return (base64.b64encode(ct).decode(), base64.b64encode(enc_key).decode(),
            base64.b64encode(iv).decode(), aes_key, iv)


def _decrypt_reply(reply_b64: str, aes_key: bytes, iv: bytes) -> dict:
    ct = base64.b64decode(reply_b64)
    flipped = bytes(b ^ 0xFF for b in iv)
    return json.loads(AESGCM(aes_key).decrypt(flipped, ct, None).decode())


async def _echo_handler(payload):
    return {"screen": "NEXT", "data": {"saw_action": payload.get("action")}}


def test_ping_is_answered_without_the_handler():
    priv, pub = _keypair()
    efd, eak, iv_b64, aes, iv = _encrypt_like_meta({"version": "3.0", "action": "ping"}, pub)

    async def _boom(payload):
        raise AssertionError("handler must not be called for ping")

    reply = run(fe.process(encrypted_flow_data=efd, encrypted_aes_key=eak, initial_vector=iv_b64,
                           private_key_pem=priv, handler=_boom))
    assert _decrypt_reply(reply, aes, iv) == {"data": {"status": "active"}}


def test_data_exchange_round_trips_through_the_handler():
    priv, pub = _keypair()
    payload = {"version": "3.0", "action": "data_exchange", "screen": "DATE",
               "flow_token": "tok_1", "data": {"date": "2026-09-10"}}
    efd, eak, iv_b64, aes, iv = _encrypt_like_meta(payload, pub)
    reply = run(fe.process(encrypted_flow_data=efd, encrypted_aes_key=eak, initial_vector=iv_b64,
                           private_key_pem=priv, handler=_echo_handler))
    assert _decrypt_reply(reply, aes, iv) == {"screen": "NEXT", "data": {"saw_action": "data_exchange"}}


def test_the_reply_uses_the_flipped_iv():
    priv, pub = _keypair()
    efd, eak, iv_b64, aes, iv = _encrypt_like_meta({"action": "ping"}, pub)
    reply = run(fe.process(encrypted_flow_data=efd, encrypted_aes_key=eak, initial_vector=iv_b64,
                           private_key_pem=priv, handler=_echo_handler))
    ct = base64.b64decode(reply)
    # decrypting with the UNFLIPPED iv must fail — proves we flipped it
    import pytest
    with pytest.raises(Exception):
        AESGCM(aes).decrypt(iv, ct, None)


def test_a_tampered_payload_is_a_clean_crypto_error():
    import pytest
    priv, pub = _keypair()
    efd, eak, iv_b64, aes, iv = _encrypt_like_meta({"action": "ping"}, pub)
    bad = base64.b64encode(base64.b64decode(efd)[:-1] + b"\x00").decode()   # corrupt the tag
    with pytest.raises(fe.FlowCryptoError):
        run(fe.process(encrypted_flow_data=bad, encrypted_aes_key=eak, initial_vector=iv_b64,
                       private_key_pem=priv, handler=_echo_handler))


def test_the_wrong_key_is_a_clean_crypto_error():
    import pytest
    _, pub = _keypair()
    other_priv, _ = _keypair()
    efd, eak, iv_b64, aes, iv = _encrypt_like_meta({"action": "ping"}, pub)
    with pytest.raises(fe.FlowCryptoError):
        run(fe.process(encrypted_flow_data=efd, encrypted_aes_key=eak, initial_vector=iv_b64,
                       private_key_pem=other_priv, handler=_echo_handler))


def test_encrypted_private_key_with_passphrase():
    priv, pub = _keypair(passphrase=b"s3cret")
    efd, eak, iv_b64, aes, iv = _encrypt_like_meta({"action": "ping"}, pub)
    reply = run(fe.process(encrypted_flow_data=efd, encrypted_aes_key=eak, initial_vector=iv_b64,
                           private_key_pem=priv, handler=_echo_handler, passphrase=b"s3cret"))
    assert _decrypt_reply(reply, aes, iv) == {"data": {"status": "active"}}
