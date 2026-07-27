"""Bootstrapping a tenant's first owner over the internal token.

The load-bearing tests: the egg actually breaks (a new tenant gets a real Person owner who holds
members:write), retries are no-ops, an existing human's credential is never touched (T2), and the
guards keep it a BOOTSTRAP primitive rather than a backdoor to a second owner on a live tenant.
"""
from __future__ import annotations

import os

import pytest
from fastapi.testclient import TestClient

ITOKEN = "t" * 40


def _make(multi_tenant="true"):
    from app.config import get_settings
    os.environ.update(ENV="test", INTERNAL_SERVICE_TOKEN=ITOKEN,
                      IDENTITY_MULTI_TENANT=multi_tenant)
    get_settings.cache_clear()
    from app.main import create_app
    return TestClient(create_app(), raise_server_exceptions=False)


@pytest.fixture
def client():
    saved = dict(os.environ)
    c = _make()
    yield c
    os.environ.clear(); os.environ.update(saved)
    from app.config import get_settings
    get_settings.cache_clear()


def _h():
    return {"X-Internal-Token": ITOKEN}


def _seed(client, **body):
    return client.post("/internal/tenancy/seed-owner", json=body, headers=_h())


# ── the egg breaks ────────────────────────────────────────────────────────────
def test_a_fresh_tenant_gets_a_real_person_owner_with_an_invite(client):
    r = _seed(client, tenant="acme", identifier="founder@acme", caps=["members:write"])
    assert r.status_code == 201, r.text
    b = r.json()
    assert b["person_id"] and b["created_person"] is True and b["seeded"] is True
    assert b["invite_token"], "a brand-new owner with no password must get a one-time invite"
    # And they can now be seen in the directory this tenant previously had no one to read.
    members = client.get("/internal/tenancy/members?tenant=acme", headers=_h()).json()["members"]
    assert any(m["identifier"] == "founder@acme" for m in members)


def test_a_password_seeds_a_usable_account_and_no_invite(client):
    r = _seed(client, tenant="beta", identifier="ceo@beta", caps=["members:write"],
              password="correct-horse-battery")
    assert r.status_code == 201 and r.json()["invite_token"] is None


# ── idempotent ────────────────────────────────────────────────────────────────
def test_re_seeding_the_same_owner_is_a_no_op(client):
    first = _seed(client, tenant="co", identifier="f@co", caps=["members:write"]).json()
    again = _seed(client, tenant="co", identifier="f@co", caps=["members:write"])
    assert again.status_code == 201
    b = again.json()
    assert b["person_id"] == first["person_id"]
    assert b["created_person"] is False and b["seeded"] is False
    assert b["invite_token"] is None            # no second invite on a retry


# ── T2: an existing human's credential is never touched ───────────────────────
def test_a_password_for_an_existing_person_is_refused(client):
    _seed(client, tenant="c1", identifier="multi@corp", caps=["members:write"])   # person now exists
    r = _seed(client, tenant="c2", identifier="multi@corp", caps=["members:write"],
              password="try-to-set-it-1234")
    assert r.status_code == 403                  # CANNOT_SET_EXISTING_PERSON_CREDENTIAL


# ── the two bootstrap guards ──────────────────────────────────────────────────
def test_an_owner_without_members_write_is_refused(client):
    r = _seed(client, tenant="d", identifier="weak@d", caps=["members:read"])
    assert r.status_code == 409                  # the egg would stay unbroken


def test_only_if_empty_blocks_a_different_second_owner(client):
    _seed(client, tenant="e", identifier="owner1@e", caps=["members:write"])
    blocked = _seed(client, tenant="e", identifier="owner2@e", caps=["members:write"])
    assert blocked.status_code == 409, "seed-owner must not add a second owner to a staffed tenant"
    # …unless the caller overrides deliberately.
    forced = _seed(client, tenant="e", identifier="owner2@e", caps=["members:write"],
                   only_if_empty=False)
    assert forced.status_code == 201 and forced.json()["seeded"] is True


# ── compensation: the seed carries an undo reference, and it revokes idempotently ──
def test_seed_returns_an_undo_id_and_revoke_compensates(client):
    seeded = _seed(client, tenant="saga", identifier="owner@saga", caps=["members:write"]).json()
    ref = seeded["id"]
    assert ref == f"saga::{seeded['person_id']}"

    # The onboarding saga's compensation: DELETE the reference. First time removes it…
    r1 = client.delete(f"/internal/tenancy/membership/{ref}", headers=_h())
    assert r1.status_code == 200 and r1.json()["revoked"] is True
    assert not client.get("/internal/tenancy/members?tenant=saga",
                          headers=_h()).json()["members"]
    # …a resumed rollback re-attempts an already-gone undo: success, not an error.
    r2 = client.delete(f"/internal/tenancy/membership/{ref}", headers=_h())
    assert r2.status_code == 200 and r2.json()["revoked"] is False


def test_revoke_needs_the_token_and_a_well_formed_reference(client):
    assert client.delete("/internal/tenancy/membership/t::p").status_code == 401
    assert client.delete("/internal/tenancy/membership/no-separator", headers=_h()).status_code == 400


# ── the tenant.owner_seeded hint (best-effort, at-most-once) ──────────────────
def test_a_real_seed_emits_tenant_owner_seeded(client):
    seen = []

    async def _handler(ev):
        seen.append(ev)

    import asyncio
    asyncio.run(client.app.state.container.event_bus.subscribe("tenant.owner_seeded", _handler))

    _seed(client, tenant="evt", identifier="owner@evt", caps=["members:write"])
    assert seen, "a successful seed must emit tenant.owner_seeded"
    ev = seen[0]
    assert ev.payload["tenant"] == "evt" and ev.payload["person_id"]
    # No raw identifier travels on the bus — person_id is the link, not the email.
    assert "owner@evt" not in str(ev.payload)


def test_a_no_op_reseed_does_not_re_emit(client):
    import asyncio
    _seed(client, tenant="evt2", identifier="o@evt2", caps=["members:write"])   # first seed
    seen = []

    async def _handler(ev):
        seen.append(ev)

    asyncio.run(client.app.state.container.event_bus.subscribe("tenant.owner_seeded", _handler))
    _seed(client, tenant="evt2", identifier="o@evt2", caps=["members:write"])   # no-op re-seed
    assert not seen, "an idempotent re-seed must not re-emit the hint"


def test_a_failing_bus_does_not_fail_the_seed(client, monkeypatch):
    async def _boom(*a, **k):
        raise RuntimeError("bus down")
    monkeypatch.setattr(client.app.state.container.event_bus, "publish", _boom)
    r = _seed(client, tenant="evt3", identifier="o@evt3", caps=["members:write"])
    assert r.status_code == 201 and r.json()["seeded"] is True   # the seed committed regardless


# ── refusals ──────────────────────────────────────────────────────────────────
def test_it_needs_the_internal_token(client):
    assert client.post("/internal/tenancy/seed-owner",
                       json={"tenant": "x", "identifier": "y@z"}).status_code == 401


def test_single_tenant_mode_refuses():
    saved = dict(os.environ)
    try:
        c = _make(multi_tenant="false")
        r = c.post("/internal/tenancy/seed-owner",
                   json={"tenant": "x", "identifier": "y@z", "caps": ["members:write"]},
                   headers=_h())
        assert r.status_code == 503             # MULTI_TENANT_OFF — refuses rather than half-acting
    finally:
        os.environ.clear(); os.environ.update(saved)
        from app.config import get_settings
        get_settings.cache_clear()
