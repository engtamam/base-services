"""The multi-tenant routes over HTTP — the wall as a caller actually meets it.

The unit tests prove the core refuses. These prove the SERVICE refuses: the same status code for
"no such tenant" as for "not your tenant", a token that really does carry one tenant, and — when
the knob is off — routes that do not exist at all.
"""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient


# Identity SIGNS with the issuer port and VERIFIES with the verifier port — two ports, two keys,
# deliberately: everywhere else in the platform the verifier is the only half that exists. For a
# symmetric HS256 deployment the two keys are the same secret, and identity is the one service that
# must configure both.
SECRET = "s" * 48
SIGNING = {
    "TOKEN_ISSUER_DRIVER": "hs256",
    "TOKEN_SIGNING_SECRET": SECRET,
    "TOKEN_VERIFIER_DRIVER": "hs256",
    "TOKEN_HS256_SECRET": SECRET,
}


@pytest.fixture
def env(monkeypatch):
    """Set config for the WHOLE test, not just app construction.

    The routes are mounted at create_app(), but `_svc()` re-reads the settings on every request —
    exactly as it does in production, where the environment obviously outlives startup. Restoring
    the env before the first request would make the app disagree with itself in a way no real
    deployment ever does.
    """
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
def mt(env) -> TestClient:
    return env(IDENTITY_MULTI_TENANT="true")


def seed(client: TestClient, tenants: dict[str, list[str]], ident="consultant",
         pw="correct-horse-battery"):
    """Create the person and their doors straight through the store — there is deliberately no
    route that lets a tenant admin create a Person (T2)."""
    import asyncio

    from app.core.membership import Membership, MembershipStore, Person

    c = client.app.state.container
    ms = MembershipStore(c.datastore)

    async def _go():
        p = await ms.create_person(Person(
            id="p1", identifier=ident, password_hash=c.password_hasher.hash(pw)))
        for tenant, caps in tenants.items():
            await ms.grant(Membership(person_id=p.id, tenant=tenant, caps=caps))
        return p

    return asyncio.run(_go())


# ── the knob is a real wall, not a flag ──────────────────────────────────────
def test_routes_do_not_exist_when_multi_tenant_is_off(env):
    """Off is the default. A single-tenant deployment must not be able to reach a half-built
    feature it has no store for — it 404s."""
    off = env(IDENTITY_MULTI_TENANT="false")
    assert off.post("/api/v1/auth/tenant", json={"tenant": "x"}).status_code == 404
    assert off.post("/api/v1/auth/person/login",
                    json={"username": "a", "password": "b"}).status_code == 404


# ── login returns doors, never data, never a token ───────────────────────────
def test_person_login_returns_doors_and_no_token(mt):
    seed(mt, {"own-shop": ["admin"], "bank": ["reports:read"]})
    r = mt.post("/api/v1/auth/person/login",
                json={"username": "consultant", "password": "correct-horse-battery"})
    assert r.status_code == 200
    body = r.json()

    assert {m["tenant"] for m in body["memberships"]} == {"own-shop", "bank"}
    # No token until a tenant is chosen: there is nothing for one to be scoped to yet.
    assert "access_token" not in body and "refresh_token" not in body
    # And nothing that leaks the person's secrets.
    assert "password_hash" not in body["person"] and "mfa_secret" not in body["person"]


def test_bad_password_is_401(mt):
    seed(mt, {"bank": []})
    r = mt.post("/api/v1/auth/person/login",
                json={"username": "consultant", "password": "wrong"})
    assert r.status_code == 401


# ── T1 — the mint ────────────────────────────────────────────────────────────
def test_t1_cannot_mint_for_a_tenant_you_do_not_belong_to(mt):
    import jwt

    seed(mt, {"own-shop": ["admin"]})
    creds = {"username": "consultant", "password": "correct-horse-battery"}

    ok = mt.post("/api/v1/auth/tenant", json={**creds, "tenant": "own-shop"})
    assert ok.status_code == 200
    claims = jwt.decode(ok.json()["access_token"], SECRET, algorithms=["HS256"],
                        options={"verify_aud": False})
    assert claims["tenant"] == "own-shop"
    assert claims["caps"] == ["admin"]

    denied = mt.post("/api/v1/auth/tenant", json={**creds, "tenant": "victim-corp"})
    assert denied.status_code == 403


# ── T6 — the two refusals must be indistinguishable ──────────────────────────
def test_t6_unknown_tenant_and_forbidden_tenant_look_identical(mt):
    seed(mt, {"own-shop": []})
    creds = {"username": "consultant", "password": "correct-horse-battery"}

    a = mt.post("/api/v1/auth/tenant", json={**creds, "tenant": "no-such-company-anywhere"})
    b = mt.post("/api/v1/auth/tenant", json={**creds, "tenant": "real-competitor-corp"})

    assert a.status_code == b.status_code == 403
    # request_id differs per request by design and carries no information about the tenant.
    # Everything that DOES describe the refusal must be byte-identical.
    strip = lambda r: {k: v for k, v in r.json().items() if k != "request_id"}  # noqa: E731
    assert strip(a) == strip(b), \
        "different bodies would tell a competitor which of their rivals this consultant works for"


# ── switching tenants ────────────────────────────────────────────────────────
def test_switch_tenant_on_an_existing_session(mt):
    import jwt

    seed(mt, {"own-shop": ["admin"], "bank": ["reports:read"]})
    first = mt.post("/api/v1/auth/tenant", json={
        "username": "consultant", "password": "correct-horse-battery", "tenant": "own-shop"})
    token = first.json()["access_token"]

    switched = mt.post("/api/v1/auth/tenant", json={"tenant": "bank"},
                       headers={"Authorization": f"Bearer {token}"})
    assert switched.status_code == 200
    claims = jwt.decode(switched.json()["access_token"], SECRET, algorithms=["HS256"],
                        options={"verify_aud": False})
    assert claims["tenant"] == "bank"
    # The shop's admin cap does NOT come with them through the door (T3).
    assert claims["caps"] == ["reports:read"]


def test_an_access_token_cannot_switch_to_a_tenant_you_lack(mt):
    seed(mt, {"own-shop": ["admin"]})
    first = mt.post("/api/v1/auth/tenant", json={
        "username": "consultant", "password": "correct-horse-battery", "tenant": "own-shop"})
    token = first.json()["access_token"]

    r = mt.post("/api/v1/auth/tenant", json={"tenant": "bank"},
                headers={"Authorization": f"Bearer {token}"})
    assert r.status_code == 403


def test_switch_without_any_proof_is_401(mt):
    seed(mt, {"own-shop": []})
    assert mt.post("/api/v1/auth/tenant", json={"tenant": "own-shop"}).status_code == 401


# ── the directory: who works HERE ────────────────────────────────────────────
def _seed_two_companies(client):
    """Two companies, three people. `boss` is an admin at acme ONLY."""
    import asyncio

    from app.core.membership import Membership, MembershipStore, Person

    c = client.app.state.container
    ms = MembershipStore(c.datastore)
    h = c.password_hasher.hash("correct-horse-battery")

    async def _go():
        for pid, ident in (("boss", "boss@acme"), ("dev", "dev@acme"), ("rival", "ceo@rival")):
            await ms.create_person(Person(id=pid, identifier=ident, password_hash=h))
        await ms.grant(Membership(person_id="boss", tenant="acme", caps=["members:read"]))
        await ms.grant(Membership(person_id="dev", tenant="acme", roles=["engineer"]))
        await ms.grant(Membership(person_id="rival", tenant="rival-corp", caps=["members:read"]))

    asyncio.run(_go())
    return ms


def _token(client, ident):
    r = client.post("/api/v1/auth/person/login",
                    json={"username": ident, "password": "correct-horse-battery"})
    tenant = r.json()["memberships"][0]["tenant"]
    r = client.post("/api/v1/auth/tenant", json={
        "username": ident, "password": "correct-horse-battery", "tenant": tenant})
    return r.json()["access_token"]


def test_members_lists_only_this_tenant(mt):
    _seed_two_companies(mt)
    r = mt.get("/api/v1/tenancy/members",
               headers={"Authorization": f"Bearer {_token(mt, 'boss@acme')}"})
    assert r.status_code == 200
    body = r.json()

    assert body["tenant"] == "acme"
    assert {m["identifier"] for m in body["members"]} == {"boss@acme", "dev@acme"}, \
        "the rival's CEO is not on acme's staff list"
    # Identity owns the directory — but it owns no secrets it is willing to hand out.
    for m in body["members"]:
        assert "password_hash" not in m and "mfa_secret" not in m


def test_members_cannot_be_asked_for_another_tenant(mt):
    """A ?tenant= would be T1 wearing a different hat: every admin enumerating every company.
    The tenant comes off the token, so the parameter is simply ignored."""
    _seed_two_companies(mt)
    r = mt.get("/api/v1/tenancy/members?tenant=rival-corp",
               headers={"Authorization": f"Bearer {_token(mt, 'boss@acme')}"})
    assert r.status_code == 200
    assert r.json()["tenant"] == "acme"
    assert all(m["identifier"] != "ceo@rival" for m in r.json()["members"])


def test_members_requires_the_capability(mt):
    """`dev` is a real member of acme with a valid token — and no members:read cap."""
    _seed_two_companies(mt)
    r = mt.get("/api/v1/tenancy/members",
               headers={"Authorization": f"Bearer {_token(mt, 'dev@acme')}"})
    assert r.status_code == 403


def test_members_requires_a_token(mt):
    _seed_two_companies(mt)
    assert mt.get("/api/v1/tenancy/members").status_code == 401


def test_members_paginates_with_a_cursor(env):
    c = env(IDENTITY_MULTI_TENANT="true", IDENTITY_MEMBERS_PAGE_SIZE="1")
    _seed_two_companies(c)
    tok = {"Authorization": f"Bearer {_token(c, 'boss@acme')}"}

    first = c.get("/api/v1/tenancy/members", headers=tok).json()
    assert len(first["members"]) == 1 and first["next"]

    second = c.get(f"/api/v1/tenancy/members?after={first['next']}", headers=tok).json()
    seen = {m["identifier"] for m in first["members"] + second["members"]}
    assert seen == {"boss@acme", "dev@acme"}, "the cursor must advance, not repeat"


def test_members_page_size_is_capped(env):
    """A caller may not ask for the whole company in one request."""
    c = env(IDENTITY_MULTI_TENANT="true", IDENTITY_MEMBERS_MAX_PAGE_SIZE="1")
    _seed_two_companies(c)
    r = c.get("/api/v1/tenancy/members?limit=1000",
              headers={"Authorization": f"Bearer {_token(c, 'boss@acme')}"})
    assert len(r.json()["members"]) == 1


def test_members_route_absent_when_multi_tenant_is_off(env):
    off = env(IDENTITY_MULTI_TENANT="false")
    assert off.get("/api/v1/tenancy/members").status_code == 404


# ── T10 — step-up on switch, stated not assumed ──────────────────────────────
def test_t10_reauth_on_switch_refuses_a_bare_session_when_enabled(env):
    """On, a stolen session may not widen itself into a higher-trust tenant by merely asking."""
    c = env(IDENTITY_MULTI_TENANT="true", IDENTITY_TENANT_SWITCH_REAUTH="true")
    seed(c, {"own-shop": [], "bank": []})
    creds = {"username": "consultant", "password": "correct-horse-battery"}

    first = c.post("/api/v1/auth/tenant", json={**creds, "tenant": "own-shop"})
    assert first.status_code == 200
    token = first.json()["access_token"]

    bare = c.post("/api/v1/auth/tenant", json={"tenant": "bank"},
                  headers={"Authorization": f"Bearer {token}"})
    assert bare.status_code == 401, "step-up is on: the session alone is not enough"

    # Re-proving the person still works — the door is closed, not walled up.
    assert c.post("/api/v1/auth/tenant", json={**creds, "tenant": "bank"}).status_code == 200
