"""AuthService.delete_self — 52.2. A person erases themselves, safely.

Two guards carry this: the SOLE-OWNER refusal (never strand a tenant) and the LAST-MEMBERSHIP
cascade (delete the Person only when they belong nowhere else — T2/CWE-640). These tests drive the
service directly so the rules are pinned independent of the route.
"""
from __future__ import annotations

import asyncio

import pytest

from app.adapters.datastore.memory import MemoryDataStore
from app.core.auth_service import AuthService, SoleOwnerError
from app.core.membership import Membership, MembershipStore, Person


def run(c):
    return asyncio.run(c)


class _Hasher:
    def hash(self, s):
        return "argon2:" + s


def _svc(store):
    return AuthService(
        users=object(), refresh=object(), hasher=_Hasher(), issuer=object(), verifier=object(),
        mfa=object(), identifier=object(), access_ttl=900, refresh_ttl=86400,
        memberships=MembershipStore(store))


def _person(ms, pid="p_1", identifier="ada@example.com", tenants=(("acme", ["member"]),)):
    p = run(ms.create_person(Person(id=pid, identifier=identifier, password_hash="argon2:x")))
    for tenant, roles in tenants:
        run(ms.grant(Membership(person_id=p.id, tenant=tenant, roles=list(roles))))
    return p


def test_last_membership_deletes_the_person():
    st = MemoryDataStore()
    svc = _svc(st)
    ms = MembershipStore(st)
    _person(ms, tenants=(("acme", ["member"]),))
    res = run(svc.delete_self("p_1", "acme"))
    assert res["membership_revoked"] and res["person_deleted"]
    assert run(ms.person("p_1")) is None


def test_a_person_who_belongs_elsewhere_is_kept():
    """T2/CWE-640: revoke the one membership, but the Person still works for beta."""
    st = MemoryDataStore()
    svc = _svc(st)
    ms = MembershipStore(st)
    _person(ms, tenants=(("acme", ["member"]), ("beta", ["member"])))
    res = run(svc.delete_self("p_1", "acme"))
    assert res["membership_revoked"] and not res["person_deleted"]
    assert run(ms.person("p_1")) is not None
    assert run(ms.membership("p_1", "acme")) is None
    assert run(ms.membership("p_1", "beta")) is not None


def test_the_sole_owner_is_refused():
    st = MemoryDataStore()
    svc = _svc(st)
    ms = MembershipStore(st)
    _person(ms, tenants=(("acme", ["owner"]),))
    with pytest.raises(SoleOwnerError):
        run(svc.delete_self("p_1", "acme"))
    # nothing was revoked — the tenant still has its owner
    assert run(ms.membership("p_1", "acme")) is not None


def test_an_owner_with_a_co_owner_may_leave():
    st = MemoryDataStore()
    svc = _svc(st)
    ms = MembershipStore(st)
    _person(ms, pid="p_1", identifier="ada@x.com", tenants=(("acme", ["owner"]),))
    _person(ms, pid="p_2", identifier="grace@x.com", tenants=(("acme", ["owner"]),))
    res = run(svc.delete_self("p_1", "acme"))
    assert res["membership_revoked"]
    assert run(ms.membership("p_1", "acme")) is None               # ada left
    assert "owner" in run(ms.membership("p_2", "acme")).roles      # grace still owns it


def test_revoking_a_missing_membership_is_not_found():
    st = MemoryDataStore()
    svc = _svc(st)
    res = run(svc.delete_self("ghost", "acme"))
    assert res["deleted"] is False and res["reason"] == "NOT_FOUND"
