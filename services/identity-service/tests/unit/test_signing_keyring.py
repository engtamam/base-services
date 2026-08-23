"""The signing keyring: kid is a stable thumbprint, rotation keeps the old key verifiable, the
overlap window governs when it finally drops, and a token verifies against the JWK it advertises."""
from __future__ import annotations

import json

import jwt
import pytest

from app.core.signing_keyring import (Keyring, SigningKey, generate_rsa_keypair,
                                       public_jwk)

# Two fixed keypairs so the tests are deterministic (no keygen inside the assertions).
_PRIV_A, _PUB_A = generate_rsa_keypair()
_PRIV_B, _PUB_B = generate_rsa_keypair()


def test_kid_is_a_stable_thumbprint_of_the_public_key():
    a1 = public_jwk(_PUB_A)["kid"]
    a2 = public_jwk(_PUB_A)["kid"]
    b = public_jwk(_PUB_B)["kid"]
    assert a1 == a2 and a1 != b                     # same key → same kid; different key → different
    assert public_jwk(_PUB_A)["alg"] == "RS256" and public_jwk(_PUB_A)["use"] == "sig"


def test_seed_is_single_key_and_current():
    ring = Keyring.seed(_PRIV_A, now=1000.0)
    assert len(ring.keys()) == 1
    assert ring.current.kid == public_jwk(_PUB_A)["kid"]
    assert ring.jwks(now=1000.0)["keys"][0]["kid"] == ring.current.kid


def test_rotate_switches_signer_but_keeps_old_key_in_the_jwks():
    ring = Keyring.seed(_PRIV_A, now=1000.0)
    old_kid = ring.current.kid
    new = ring.rotate(now=2000.0, overlap_seconds=600.0,
                      keygen=lambda: (_PRIV_B, _PUB_B))
    assert ring.current.kid == new.kid != old_kid
    kids = {k["kid"] for k in ring.jwks(now=2000.0)["keys"]}
    assert kids == {old_kid, new.kid}, "both keys verifiable during the overlap"


def test_old_key_drops_from_jwks_only_after_the_overlap_lapses():
    ring = Keyring.seed(_PRIV_A, now=1000.0)
    old_kid = ring.current.kid
    ring.rotate(now=2000.0, overlap_seconds=600.0, keygen=lambda: (_PRIV_B, _PUB_B))
    assert old_kid in {k["kid"] for k in ring.jwks(now=2599.0)["keys"]}   # still inside window
    assert old_kid not in {k["kid"] for k in ring.jwks(now=2601.0)["keys"]}  # window lapsed


def test_prune_never_drops_the_current_key():
    ring = Keyring.seed(_PRIV_A, now=1000.0)
    ring.rotate(now=2000.0, overlap_seconds=600.0, keygen=lambda: (_PRIV_B, _PUB_B))
    # Far in the future: the retired key is gone, the current signer survives.
    ring.prune(now=10_000.0)
    assert [k.kid for k in ring.keys()] == [ring.current.kid]


def test_rotate_refuses_zero_overlap():
    ring = Keyring.seed(_PRIV_A, now=1000.0)
    with pytest.raises(ValueError):
        ring.rotate(now=2000.0, overlap_seconds=0, keygen=lambda: (_PRIV_B, _PUB_B))


def test_round_trips_through_json():
    ring = Keyring.seed(_PRIV_A, now=1000.0)
    ring.rotate(now=2000.0, overlap_seconds=600.0, keygen=lambda: (_PRIV_B, _PUB_B))
    back = Keyring.from_json(ring.to_json())
    assert back.current.kid == ring.current.kid
    assert {k.kid for k in back.keys()} == {k.kid for k in ring.keys()}
    # the retiring key's deadline survives the trip
    assert {k.retire_after for k in back.keys()} == {k.retire_after for k in ring.keys()}


def test_a_token_verifies_against_the_advertised_jwk():
    """End-to-end: sign with the current private key stamping its kid, then verify by selecting the
    matching JWK from the published set — exactly what a fleet verifier does via PyJWKClient."""
    ring = Keyring.seed(_PRIV_A, now=1000.0)
    ring.rotate(now=2000.0, overlap_seconds=600.0, keygen=lambda: (_PRIV_B, _PUB_B))
    cur = ring.current
    token = jwt.encode({"sub": "u1"}, cur.private_pem, algorithm="RS256",
                       headers={"kid": cur.kid})
    # verifier side: find the JWK whose kid matches the token header, build a key, verify
    hdr = jwt.get_unverified_header(token)
    jwk = next(k for k in ring.jwks(now=2100.0)["keys"] if k["kid"] == hdr["kid"])
    pub = jwt.algorithms.RSAAlgorithm.from_jwk(json.dumps(jwk))
    assert jwt.decode(token, pub, algorithms=["RS256"])["sub"] == "u1"
    # a token signed by the OLD key still verifies while the old JWK is published
    old_token = jwt.encode({"sub": "u2"}, _PRIV_A, algorithm="RS256",
                           headers={"kid": public_jwk(_PUB_A)["kid"]})
    old_jwk = next(k for k in ring.jwks(now=2100.0)["keys"]
                   if k["kid"] == public_jwk(_PUB_A)["kid"])
    old_pub = jwt.algorithms.RSAAlgorithm.from_jwk(json.dumps(old_jwk))
    assert jwt.decode(old_token, old_pub, algorithms=["RS256"])["sub"] == "u2"
