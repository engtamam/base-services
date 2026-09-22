"""Regression: /auth/change-password and /auth/me must work for a PERSON (multi-tenant) account.

Real accounts are created as Persons via seed_owner/add_member (never /auth/register), so they have
no UserRepository row. Before the fix both routes resolved UserRepository only and 404'd for EVERY
real account. These drive the real HTTP flow (person login → tenant token → change password / me).
"""
from __future__ import annotations

import asyncio

import pytest
from fastapi.testclient import TestClient

SECRET = "s" * 48
SIGNING = {"TOKEN_ISSUER_DRIVER": "hs256", "TOKEN_SIGNING_SECRET": SECRET,
           "TOKEN_VERIFIER_DRIVER": "hs256", "TOKEN_HS256_SECRET": SECRET, "PLATFORM_TENANT": "platform",
           "PII_SALT": "p" * 32}
PW = "correct-horse-battery"


@pytest.fixture
def env(monkeypatch):
    from app.config import get_settings

    def _set(**kw):
        for k, v in {**SIGNING, **kw}.items():
            monkeypatch.setenv(k, str(v))
        get_settings.cache_clear()
        from app.main import create_app
        return TestClient(create_app())

    yield _set
    get_settings.cache_clear()


@pytest.fixture
def mt(env):
    return env(IDENTITY_MULTI_TENANT="true")


def _seed_person(client, ident="owner@acme", pw=PW):
    from app.core.membership import Membership, MembershipStore, Person
    c = client.app.state.container
    ms = MembershipStore(c.datastore)

    async def _go():
        p = await ms.create_person(Person(id="p1", identifier=ident,
                                          password_hash=c.password_hasher.hash(pw)))
        await ms.grant(Membership(person_id=p.id, tenant="acme", caps=["admin"]))
        return p
    return asyncio.run(_go())


def _token(client, ident="owner@acme", pw=PW):
    client.post("/api/v1/auth/person/login", json={"username": ident, "password": pw})
    r = client.post("/api/v1/auth/tenant", json={"username": ident, "password": pw, "tenant": "acme"})
    return r.json()["access_token"]


def _auth(tok):
    return {"Authorization": f"Bearer {tok}"}


# ── the bug ──────────────────────────────────────────────────────────────────────
def test_person_can_change_password_end_to_end(mt):
    _seed_person(mt)
    tok = _token(mt)
    r = mt.post("/api/v1/auth/change-password",
                json={"old_password": PW, "new_password": "a-brand-new-secret-1"}, headers=_auth(tok))
    assert r.status_code == 200, r.text                     # was 404 before the fix
    # the new password now logs in, and the old one no longer does
    assert mt.post("/api/v1/auth/person/login",
                   json={"username": "owner@acme", "password": "a-brand-new-secret-1"}
                   ).status_code == 200
    assert mt.post("/api/v1/auth/person/login",
                   json={"username": "owner@acme", "password": PW}).status_code == 401


def test_person_me_returns_the_person_not_404(mt):
    _seed_person(mt)
    r = mt.get("/api/v1/auth/me", headers=_auth(_token(mt)))
    assert r.status_code == 200, r.text                     # was 404 before the fix
    body = r.json()
    assert body["identifier"] == "owner@acme" and body["id"] == "p1"
    assert "password_hash" not in body and "mfa_secret" not in body


def test_person_change_password_wrong_old_is_400_not_404(mt):
    _seed_person(mt)
    r = mt.post("/api/v1/auth/change-password",
                json={"old_password": "wrong-one", "new_password": "another-good-secret-9"},
                headers=_auth(_token(mt)))
    assert r.status_code == 400   # 400 (wrong password), NOT 404 — the bug; detail masked (CWE-209)
    # the real password still works (nothing was changed)
    assert mt.post("/api/v1/auth/person/login",
                   json={"username": "owner@acme", "password": PW}).status_code == 200


def test_person_change_password_weak_new_is_400_weak(mt):
    _seed_person(mt)
    r = mt.post("/api/v1/auth/change-password",
                json={"old_password": PW, "new_password": "x"}, headers=_auth(_token(mt)))
    assert r.status_code == 400   # rejected as weak (not 404); specific reason masked in the body


# ── the single-tenant User path must still work (no regression) ───────────────────
def test_single_tenant_user_change_password_still_works(env):
    c = env(IDENTITY_MULTI_TENANT="false")
    reg = c.post("/api/v1/auth/register", json={"username": "solo@x", "password": PW})
    assert reg.status_code in (200, 201), reg.text
    login = c.post("/api/v1/auth/login", json={"username": "solo@x", "password": PW})
    tok = login.json()["access_token"]
    r = c.post("/api/v1/auth/change-password",
               json={"old_password": PW, "new_password": "solo-new-secret-2"}, headers=_auth(tok))
    assert r.status_code == 200, r.text
    assert c.get("/api/v1/auth/me", headers=_auth(tok)).status_code == 200
