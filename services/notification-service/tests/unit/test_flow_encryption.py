"""The Flow encryption keypair: generated keys are in the formats Meta and flow_crypto both need, and
a generated key actually decrypts a Meta-style encrypted request end to end."""
from __future__ import annotations

import base64
import json
import os

from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from app.core import flow_crypto
from app.core.flow_encryption import (EncryptionKeyResult, generate_keypair,
                                      public_key_from_private)


def test_generate_keypair_emits_pkcs8_private_and_spki_public():
    priv, pub = generate_keypair(2048)
    assert priv.startswith("-----BEGIN PRIVATE KEY-----")          # PKCS#8 (flow_crypto loads it)
    assert pub.startswith("-----BEGIN PUBLIC KEY-----")            # SPKI (Meta's endpoint wants this)
    # both parse, and the public half derived from the private matches the emitted public half
    assert public_key_from_private(priv) == pub


def test_generated_key_decrypts_a_meta_style_request():
    # Prove the generated keypair is usable end to end: encrypt exactly as Meta does (RSA-OAEP-SHA256
    # wrapping a 128-bit AES key, AES-GCM payload with the tag appended) to the PUBLIC key, then decrypt
    # with flow_crypto using the PRIVATE key. If the formats were wrong this would raise.
    priv, pub = generate_keypair(2048)
    aes, iv = os.urandom(16), os.urandom(12)
    public_key = serialization.load_pem_public_key(pub.encode())
    ek = public_key.encrypt(aes, padding.OAEP(mgf=padding.MGF1(hashes.SHA256()),
                                              algorithm=hashes.SHA256(), label=None))
    ct = AESGCM(aes).encrypt(iv, json.dumps({"action": "ping"}).encode(), None)

    payload, got_key, got_iv = flow_crypto.decrypt_request(
        encrypted_flow_data=base64.b64encode(ct).decode(),
        encrypted_aes_key=base64.b64encode(ek).decode(),
        initial_vector=base64.b64encode(iv).decode(),
        private_key_pem=priv.encode())
    assert payload == {"action": "ping"} and got_key == aes and got_iv == iv


def test_result_public_hides_nothing_secret():
    out = EncryptionKeyResult(ok=True, created=True, uploaded=True,
                              phone_number_id="pn1", status="VALID").public()
    assert out == {"ok": True, "created": True, "uploaded": True,
                   "phone_number_id": "pn1", "status": "VALID"}
    # a failure carries a reason; a success does not
    assert "reason" in EncryptionKeyResult(ok=False, reason="HTTP 403").public()
