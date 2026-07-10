"""Unit tests for identity core: hasher, issuer, repo, auth service."""
from __future__ import annotations

import asyncio

import pytest

from app.adapters.datastore.memory import MemoryDataStore
from app.adapters.mfa.null import NullMfa
from app.adapters.password_hasher.argon2 import Argon2Hasher
from app.adapters.token_issuer.hs256 import HS256Issuer
from app.adapters.token_verifier.hs256 import HS256Verifier
from app.core.auth_service import AuthService, WeakPassword
from app.core.refresh_store import RefreshStore
from app.core.user_repository import UserExists, UserRepository

SEC = "k" * 40


def run(c):
    return asyncio.run(c)


def _svc():
    store = MemoryDataStore()
    return AuthService(
        users=UserRepository(store), refresh=RefreshStore(store),
        hasher=Argon2Hasher(), issuer=HS256Issuer(SEC), verifier=HS256Verifier(SEC),
        mfa=NullMfa(), access_ttl=900, refresh_ttl=3600,
    )


def test_hasher_roundtrip():
    h = Argon2Hasher()
    hashed = h.hash("s3cret!!")
    assert hashed != "s3cret!!"
    assert h.verify("s3cret!!", hashed) is True
    assert h.verify("wrong", hashed) is False


def test_issuer_access_verifies():
    tok = HS256Issuer(SEC).issue_access("u1", {"caps": ["read"]}, 900)
    claims = HS256Verifier(SEC).verify(tok)
    assert claims["sub"] == "u1" and claims["type"] == "access" and claims["caps"] == ["read"]


def test_register_and_authenticate():
    svc = _svc()
    u = run(svc.register("t", "alice", "password1", ["read"]))
    assert u.username == "alice"
    assert run(svc.authenticate("t", "alice", "password1")) is not None
    assert run(svc.authenticate("t", "alice", "wrong")) is None
    assert run(svc.authenticate("t", "ghost", "password1")) is None  # unknown user


def test_duplicate_register_rejected():
    svc = _svc()
    run(svc.register("t", "bob", "password1"))
    with pytest.raises(UserExists):
        run(svc.register("t", "bob", "password1"))


def test_weak_password_rejected():
    with pytest.raises(WeakPassword):
        run(_svc().register("t", "x", "short"))


def test_refresh_rotation_invalidates_old():
    svc = _svc()
    u = run(svc.register("t", "carol", "password1"))
    toks = run(svc.issue_tokens(u))
    rotated = run(svc.refresh(toks["refresh_token"]))
    assert rotated is not None and rotated["access_token"]
    # the old refresh token can never be reused (rotation)
    assert run(svc.refresh(toks["refresh_token"])) is None


def test_logout_revokes_refresh():
    svc = _svc()
    u = run(svc.register("t", "dave", "password1"))
    toks = run(svc.issue_tokens(u))
    assert run(svc.logout(toks["refresh_token"])) is True
    assert run(svc.refresh(toks["refresh_token"])) is None


def test_change_password():
    svc = _svc()
    u = run(svc.register("t", "erin", "password1"))
    assert run(svc.change_password(u, "password1", "password2")) is True
    assert run(svc.authenticate("t", "erin", "password2")) is not None
    assert run(svc.authenticate("t", "erin", "password1")) is None
