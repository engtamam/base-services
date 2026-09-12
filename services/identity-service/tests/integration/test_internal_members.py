"""A background job (internal token, no user JWT) can read the member directory — scoped to the
tenant it names, and never leaking a credential."""
from __future__ import annotations

import asyncio
import os

import pytest
from fastapi.testclient import TestClient

ITOKEN = "t" * 40


@pytest.fixture
def client(monkeypatch):
    for k, v in {"ENV": "test", "INTERNAL_SERVICE_TOKEN": ITOKEN,
                 "IDENTITY_MULTI_TENANT": "true", "PLATFORM_TENANT": "platform"}.items():
        monkeypatch.setenv(k, v)
    from app.config import get_settings
    get_settings.cache_clear()
    from app.main import create_app
    c = TestClient(create_app(), raise_server_exceptions=False)

    import asyncio as _a
    from app.core.membership import Membership, MembershipStore, Person
    ms = MembershipStore(c.app.state.container.datastore)

    async def seed():
        for pid, ident, tn in (("a", "boss@acme", "acme"), ("b", "dev@acme", "acme"),
                               ("c", "ceo@rival", "rival-corp")):
            await ms.create_person(Person(id=pid, identifier=ident,
                                          password_hash=c.app.state.container.password_hasher.hash("x" * 12)))
            await ms.grant(Membership(person_id=pid, tenant=tn, roles=["r"]))
    _a.get_event_loop().run_until_complete(seed()) if False else _a.run(seed())
    yield c
    get_settings.cache_clear()


def _h():
    return {"X-Internal-Token": ITOKEN}


def test_a_background_job_can_read_members_with_the_internal_token(client):
    r = client.get("/internal/tenancy/members?tenant=acme", headers=_h())
    assert r.status_code == 200
    idents = {m["identifier"] for m in r.json()["members"]}
    assert idents == {"boss@acme", "dev@acme"}


def test_it_is_scoped_to_the_named_tenant(client):
    r = client.get("/internal/tenancy/members?tenant=rival-corp", headers=_h())
    assert {m["identifier"] for m in r.json()["members"]} == {"ceo@rival"}
    # acme's job cannot see rival's people and vice versa — the tenant param scopes it.
    acme = client.get("/internal/tenancy/members?tenant=acme", headers=_h())
    assert all(m["identifier"] != "ceo@rival" for m in acme.json()["members"])


def test_the_tenant_is_required_no_all_tenants_dump(client):
    """There is deliberately no 'all tenants' value — a forgotten tenant fails loudly, it does not
    dump every company's staff."""
    assert client.get("/internal/tenancy/members", headers=_h()).status_code == 422


def test_it_needs_the_internal_token(client):
    assert client.get("/internal/tenancy/members?tenant=acme").status_code == 401


def test_no_credential_leaks(client):
    m = client.get("/internal/tenancy/members?tenant=acme", headers=_h()).json()["members"][0]
    assert "password_hash" not in m and "mfa_secret" not in m
