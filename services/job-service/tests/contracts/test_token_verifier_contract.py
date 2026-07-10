"""TokenVerifier contract — hs256 + rs256 (both zero-infra)."""
from __future__ import annotations

import datetime as dt

import jwt
import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa

from app.adapters.token_verifier.hs256 import HS256Verifier
from app.adapters.token_verifier.rs256 import RS256Verifier
from app.ports.errors import InvalidToken

HS_SECRET = "s" * 40
ISS = "test-issuer"
AUD = "test-aud"


def _claims(**over):
    now = dt.datetime.now(dt.timezone.utc)
    base = {
        "sub": "user-1",
        "iss": ISS,
        "aud": AUD,
        "exp": now + dt.timedelta(minutes=5),
        "iat": now,
    }
    base.update(over)
    return base


@pytest.fixture
def rsa_keys():
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    priv = key.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption(),
    ).decode()
    pub = key.public_key().public_bytes(
        serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo
    ).decode()
    return priv, pub


def test_hs256_valid(monkeypatch):
    token = jwt.encode(_claims(), HS_SECRET, algorithm="HS256")
    claims = HS256Verifier(HS_SECRET, ISS, AUD).verify(token)
    assert claims["sub"] == "user-1"


def test_hs256_expired():
    token = jwt.encode(
        _claims(exp=dt.datetime.now(dt.timezone.utc) - dt.timedelta(minutes=1)),
        HS_SECRET,
        algorithm="HS256",
    )
    with pytest.raises(InvalidToken):
        HS256Verifier(HS_SECRET, ISS, AUD).verify(token)


def test_hs256_bad_signature():
    token = jwt.encode(_claims(), "w" * 40, algorithm="HS256")
    with pytest.raises(InvalidToken):
        HS256Verifier(HS_SECRET, ISS, AUD).verify(token)


def test_rs256_valid(rsa_keys):
    priv, pub = rsa_keys
    token = jwt.encode(_claims(), priv, algorithm="RS256")
    claims = RS256Verifier(pub, ISS, AUD).verify(token)
    assert claims["sub"] == "user-1"


def test_rs256_expired(rsa_keys):
    priv, pub = rsa_keys
    token = jwt.encode(
        _claims(exp=dt.datetime.now(dt.timezone.utc) - dt.timedelta(minutes=1)),
        priv,
        algorithm="RS256",
    )
    with pytest.raises(InvalidToken):
        RS256Verifier(pub, ISS, AUD).verify(token)
