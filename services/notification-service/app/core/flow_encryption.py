"""Flow encryption key setup — the twin of ensure_flow, for the E2E-encrypted data endpoint.

A dynamic Flow's data endpoint is end-to-end encrypted (see flow_crypto): Meta encrypts every request
to a PUBLIC key it holds for the phone number, and we decrypt with the matching PRIVATE key sealed in
our secret store. Someone has to create that keypair once, keep the private half secret, and register
the public half with Meta. This module owns the pure crypto for that; the internal route orchestrates
seal + upload idempotently, exactly beside the flow-provision call at connect time.

BOUNDARY: we register the public key with Meta AS THE TENANT, using its OWN sealed Graph token (the
same conduit as flow/template provisioning) — never our own identity. The private key is generated
here, sealed under the KEK at rest, and never leaves the process in plaintext or crosses an HTTP
response. Meta's endpoint wants the PUBLIC key in SPKI PEM ("BEGIN PUBLIC KEY"); flow_crypto loads the
PKCS#8 PRIVATE PEM ("BEGIN PRIVATE KEY") we seal.

Verified against Meta's Business Encryption API reference (2026-09-08): POST
/{phone_number_id}/whatsapp_business_encryption, form field `business_public_key`, one active key per
number (a new upload replaces the previous), GET reports business_public_key_signature_status.
"""
from __future__ import annotations

from dataclasses import dataclass

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa

# Meta requires an RSA key; 2048 bits is the standard for this endpoint. Kept as a constant (not a
# magic number at the call site) and overridable by the caller for tests.
DEFAULT_KEY_SIZE = 2048
_PUBLIC_EXPONENT = 65537


@dataclass(frozen=True)
class EncryptionKeyResult:
    """The outcome the internal route returns — mirrors FlowResult.public()'s shape. Never carries the
    private key or any secret material."""
    ok: bool
    created: bool = False          # a NEW keypair was generated + sealed this call (vs. already present)
    uploaded: bool = False         # the public key was (re)registered with Meta this call
    phone_number_id: str = ""
    status: str = ""               # Meta's signature status when read back (VALID | MISMATCH | "")
    reason: str = ""

    def public(self) -> dict:
        out = {"ok": self.ok, "created": self.created, "uploaded": self.uploaded,
               "phone_number_id": self.phone_number_id, "status": self.status}
        if self.reason:
            out["reason"] = self.reason
        return out


def generate_keypair(key_size: int = DEFAULT_KEY_SIZE) -> tuple[str, str]:
    """A fresh RSA keypair as (private_pem_pkcs8, public_pem_spki).

    Private is PKCS#8, unencrypted PEM ("BEGIN PRIVATE KEY") — flow_crypto.decrypt_request loads it,
    and it is sealed under the KEK at rest by the secret store, so a second passphrase would only add
    an operational footgun. Public is SubjectPublicKeyInfo PEM ("BEGIN PUBLIC KEY") — the exact format
    Meta's business-encryption endpoint expects."""
    key = rsa.generate_private_key(public_exponent=_PUBLIC_EXPONENT, key_size=key_size)
    private_pem = key.private_bytes(
        serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption()).decode("ascii")
    public_pem = key.public_key().public_bytes(
        serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo).decode("ascii")
    return private_pem, public_pem


def public_key_from_private(private_pem: str, *, passphrase: bytes | None = None) -> str:
    """Derive the SPKI public PEM from a sealed private key — so a re-run whose key already exists can
    (re)register the SAME public key with Meta without generating (and thus rotating) anything."""
    key = serialization.load_pem_private_key(private_pem.encode("ascii"), password=passphrase)
    return key.public_key().public_bytes(
        serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo).decode("ascii")
