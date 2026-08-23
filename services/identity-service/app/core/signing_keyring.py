"""The token SIGNING keyring — the platform's asymmetric key material, rotated with an overlap.

WHY THIS EXISTS. Identity signs every token the whole platform trusts. `operator_credentials.py`
spells out the historical position: changing the signing key "invalidates every token already
issued ... a deploy-time migration with a key-overlap plan, not a form field." This module IS that
key-overlap plan, made routine. A signing key is not rotated by swapping one PEM for another — it is
rotated by minting a NEW key, signing with it from now on, and keeping the OLD public key
*verifiable* until every token it ever signed has expired. Only then is the old key dropped.

THE OVERLAP IS SIZED BY THE LONGEST-LIVED TOKEN, NOT THE SHORTEST. A refresh token lives
`REFRESH_TTL_SECONDS` (7 days by default) and is signed by the same key as a 15-minute access token.
Retiring a public key one hour after rotation would reject week-old refresh tokens that are still
perfectly valid. So the caller sizes the overlap off the refresh TTL (plus a margin), never the
access TTL. Get this wrong and a rotation logs people out — the exact failure this whole design
exists to prevent.

THE PRIVATE KEYS NEVER LEAVE THIS PROCESS. Only the PUBLIC halves are published, as a JWKS the rest
of the fleet reads to verify. This module is pure domain: it mints, rotates, prunes, and serialises
key material with no I/O and no clock of its own — the caller supplies `now` and the keygen — so it
is exhaustively testable. Persistence (sealed, at rest) and caching live in the service layer.

Each key carries a `kid` (RFC 7638 JWK thumbprint): the token header names its `kid`, and a verifier
selects the matching public key from the JWKS. A rotation that reused a kid would let a new key
masquerade as an old one, so the kid is DERIVED from the key material and never assigned.
"""
from __future__ import annotations

import base64
import dataclasses
import hashlib
import json
from typing import Any, Callable


def _b64u(b: bytes) -> str:
    return base64.urlsafe_b64encode(b).rstrip(b"=").decode("ascii")


def public_jwk(public_pem: str) -> dict[str, str]:
    """The public key as a JWK dict (kty/n/e/kid/use/alg). kid is the RFC 7638 thumbprint."""
    from jwt.algorithms import RSAAlgorithm

    # prepare_key loads the PEM into a key object; to_jwk emits its {kty,n,e} as JSON.
    key_obj = RSAAlgorithm(RSAAlgorithm.SHA256).prepare_key(public_pem)
    jwk = json.loads(RSAAlgorithm.to_jwk(key_obj))
    # to_jwk emits at least {kty,n,e}. The thumbprint is over the REQUIRED members only, in
    # lexicographic order, with no whitespace — RFC 7638 §3. Anything else changes the kid.
    canonical = json.dumps({"e": jwk["e"], "kty": jwk["kty"], "n": jwk["n"]},
                           separators=(",", ":"), sort_keys=True).encode("ascii")
    kid = _b64u(hashlib.sha256(canonical).digest())
    return {"kty": jwk["kty"], "n": jwk["n"], "e": jwk["e"], "kid": kid,
            "use": "sig", "alg": "RS256"}


def generate_rsa_keypair(bits: int = 2048) -> tuple[str, str]:
    """A fresh RSA keypair as (private_pem, public_pem). 2048 is the ASVS floor for RSA signing."""
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric import rsa

    key = rsa.generate_private_key(public_exponent=65537, key_size=bits)
    private_pem = key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption()).decode("ascii")
    public_pem = key.public_key().public_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PublicFormat.SubjectPublicKeyInfo).decode("ascii")
    return private_pem, public_pem


def _public_from_private(private_pem: str) -> str:
    from cryptography.hazmat.primitives import serialization

    key = serialization.load_pem_private_key(private_pem.encode("utf-8"), password=None)
    return key.public_key().public_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PublicFormat.SubjectPublicKeyInfo).decode("ascii")


@dataclasses.dataclass(frozen=True)
class SigningKey:
    """One keypair in the ring. `retire_after` is None while it is (or was) the current signer, and
    a wall-clock deadline once it has been superseded: past it, the public key drops from the JWKS."""

    kid: str
    private_pem: str
    public_pem: str
    created_at: float
    retire_after: float | None = None

    @classmethod
    def from_private(cls, private_pem: str, created_at: float,
                     retire_after: float | None = None) -> "SigningKey":
        public_pem = _public_from_private(private_pem)
        return cls(kid=public_jwk(public_pem)["kid"], private_pem=private_pem,
                   public_pem=public_pem, created_at=created_at, retire_after=retire_after)


class Keyring:
    """The current signer plus every retiring key still inside its verify-only overlap.

    Invariants held at every transition:
      * exactly one CURRENT key (the signer); it has retire_after=None.
      * a retiring key stays verifiable (published in the JWKS) until its retire_after passes.
      * the current key is NEVER pruned, whatever the clock says.
    """

    def __init__(self, keys: list[SigningKey], current_kid: str) -> None:
        if not keys:
            raise ValueError("a keyring needs at least one key")
        if not any(k.kid == current_kid for k in keys):
            raise ValueError("current_kid is not present in the keyring")
        # De-dup by kid defensively — two identical keys would publish two identical JWKs.
        self._keys: dict[str, SigningKey] = {}
        for k in keys:
            self._keys[k.kid] = k
        self._current_kid = current_kid

    # ── construction ────────────────────────────────────────────────────────────
    @classmethod
    def seed(cls, private_pem: str, now: float) -> "Keyring":
        """A single-key ring from an existing PEM — the back-compat boot from `TOKEN_PRIVATE_KEY`."""
        k = SigningKey.from_private(private_pem, created_at=now)
        return cls([k], k.kid)

    # ── read ──────────────────────────────────────────────────────────────────────
    @property
    def current(self) -> SigningKey:
        return self._keys[self._current_kid]

    def keys(self) -> list[SigningKey]:
        return list(self._keys.values())

    def jwks(self, now: float) -> dict[str, list[dict[str, str]]]:
        """The public key set to publish: current + every retiring key still inside its window."""
        out = []
        for k in self._keys.values():
            if k.kid == self._current_kid or k.retire_after is None or now < k.retire_after:
                out.append(public_jwk(k.public_pem))
        return {"keys": out}

    # ── transitions ────────────────────────────────────────────────────────────────
    def rotate(self, *, now: float, overlap_seconds: float,
               keygen: Callable[[], tuple[str, str]] = generate_rsa_keypair) -> "SigningKey":
        """Mint a new key, make it current, and keep the old one verify-only for the overlap.

        Returns the new current key. The previous current key is retired at now+overlap_seconds —
        the caller MUST size overlap_seconds ≥ the longest token TTL (refresh, not access) or a
        still-valid token signed by the old key will be rejected once its window lapses.
        """
        if overlap_seconds <= 0:
            raise ValueError("overlap_seconds must be positive — a zero overlap is a hard cutover")
        private_pem, _ = keygen()
        new = SigningKey.from_private(private_pem, created_at=now)
        if new.kid in self._keys:
            raise ValueError("keygen produced a kid already in the ring — refusing to rotate")
        old = self._keys[self._current_kid]
        self._keys[old.kid] = dataclasses.replace(old, retire_after=now + overlap_seconds)
        self._keys[new.kid] = new
        self._current_kid = new.kid
        self.prune(now)
        return new

    def prune(self, now: float) -> int:
        """Drop retiring keys whose window has lapsed. Never drops the current key. Returns count."""
        drop = [kid for kid, k in self._keys.items()
                if kid != self._current_kid and k.retire_after is not None and now >= k.retire_after]
        for kid in drop:
            del self._keys[kid]
        return len(drop)

    # ── persistence shape ────────────────────────────────────────────────────────────
    def to_dict(self) -> dict[str, Any]:
        return {"current_kid": self._current_kid,
                "keys": [dataclasses.asdict(k) for k in self._keys.values()]}

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "Keyring":
        keys = [SigningKey(kid=k["kid"], private_pem=k["private_pem"], public_pem=k["public_pem"],
                           created_at=float(k["created_at"]),
                           retire_after=(None if k.get("retire_after") is None
                                         else float(k["retire_after"])))
                for k in data["keys"]]
        return cls(keys, data["current_kid"])

    def to_json(self) -> str:
        return json.dumps(self.to_dict(), separators=(",", ":"), sort_keys=True)

    @classmethod
    def from_json(cls, text: str) -> "Keyring":
        return cls.from_dict(json.loads(text))
