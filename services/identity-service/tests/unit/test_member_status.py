"""AuthService.set_member_status — suspend / reactivate a member without deleting them."""
from __future__ import annotations

import asyncio

import pytest

from app.adapters.datastore.memory import MemoryDataStore
from app.core.auth_service import AuthService
from app.core.membership import ACTIVE, SUSPENDED, Membership, MembershipStore, Person
from app.core.tenancy import TenantAccessDenied, authorize_tenant


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


def _member(ms, pid="p_1", tenant="acme", roles=("member",)):
    p = run(ms.create_person(Person(id=pid, identifier="ada@example.com", password_hash="argon2:x")))
    run(ms.grant(Membership(person_id=p.id, tenant=tenant, roles=list(roles))))
    return p


def test_suspend_keeps_the_member_but_revokes_access():
    st = MemoryDataStore()
    ms = MembershipStore(st)
    svc = _svc(st)
    person = _member(ms)
    m = run(svc.set_member_status("acme", "p_1", SUSPENDED))
    assert m.status == SUSPENDED
    # the membership row and its roles survive — nothing was deleted
    assert run(ms.membership("p_1", "acme")).roles == ["member"]
    # and access is gone: authorize_tenant refuses a suspended membership (the refresh seam, T4)
    with pytest.raises(TenantAccessDenied):
        run(authorize_tenant(ms, person, "acme"))


def test_reactivate_restores_access():
    st = MemoryDataStore()
    ms = MembershipStore(st)
    svc = _svc(st)
    person = _member(ms)
    run(svc.set_member_status("acme", "p_1", SUSPENDED))
    run(svc.set_member_status("acme", "p_1", ACTIVE))
    m = run(authorize_tenant(ms, person, "acme"))     # no longer refused
    assert m.status == ACTIVE


def test_setting_the_same_status_is_idempotent():
    st = MemoryDataStore()
    ms = MembershipStore(st)
    svc = _svc(st)
    _member(ms)
    a = run(svc.set_member_status("acme", "p_1", SUSPENDED))
    b = run(svc.set_member_status("acme", "p_1", SUSPENDED))
    assert a.status == b.status == SUSPENDED


def test_non_member_returns_none():
    st = MemoryDataStore()
    svc = _svc(st)
    assert run(svc.set_member_status("acme", "ghost", SUSPENDED)) is None


def test_an_invalid_status_is_refused():
    st = MemoryDataStore()
    ms = MembershipStore(st)
    svc = _svc(st)
    _member(ms)
    with pytest.raises(ValueError):
        run(svc.set_member_status("acme", "p_1", "deleted"))
