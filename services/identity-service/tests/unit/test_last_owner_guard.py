"""No admin action may leave a tenant with zero owners — remove / role-change / suspend.

The production bug: the sole-owner invariant guarded DELETE /auth/me and tenant/close but NOT the
admin member routes, so an owner removing (or de-owning, or suspending) the LAST owner stranded the
tenant — recoverable only by a manual datastore write. These pin the guard on all three service
methods, and prove the actions that CANNOT orphan are still allowed.
"""
from __future__ import annotations

import asyncio

import pytest

from app.adapters.datastore.memory import MemoryDataStore
from app.core.auth_service import AuthService, SoleOwnerError
from app.core.membership import ACTIVE, SUSPENDED, Membership, MembershipStore, Person


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


def _member(ms, pid, roles, tenant="acme"):
    run(ms.create_person(Person(id=pid, identifier=f"{pid}@x.com", password_hash="argon2:x")))
    run(ms.grant(Membership(person_id=pid, tenant=tenant, roles=list(roles))))


# ── remove ────────────────────────────────────────────────────────────────────
def test_removing_the_last_owner_is_refused():
    st = MemoryDataStore(); ms = MembershipStore(st); svc = _svc(st)
    _member(ms, "owner1", ["owner"])
    _member(ms, "member1", ["member"])          # a non-owner does not count
    with pytest.raises(SoleOwnerError):
        run(svc.remove_member("acme", "owner1"))
    assert run(ms.membership("owner1", "acme")) is not None   # nothing removed


def test_removing_an_owner_with_a_co_owner_is_allowed():
    st = MemoryDataStore(); ms = MembershipStore(st); svc = _svc(st)
    _member(ms, "owner1", ["owner"])
    _member(ms, "owner2", ["owner"])
    assert run(svc.remove_member("acme", "owner1")) is True


def test_removing_a_non_owner_is_always_allowed():
    st = MemoryDataStore(); ms = MembershipStore(st); svc = _svc(st)
    _member(ms, "owner1", ["owner"])
    _member(ms, "member1", ["member"])
    assert run(svc.remove_member("acme", "member1")) is True


# ── role change ───────────────────────────────────────────────────────────────
def test_stripping_owner_from_the_last_owner_is_refused():
    st = MemoryDataStore(); ms = MembershipStore(st); svc = _svc(st)
    _member(ms, "owner1", ["owner"])
    with pytest.raises(SoleOwnerError):
        run(svc.update_member_roles("acme", "owner1", roles=["member"]))


def test_role_change_that_keeps_owner_is_allowed():
    st = MemoryDataStore(); ms = MembershipStore(st); svc = _svc(st)
    _member(ms, "owner1", ["owner"])
    m = run(svc.update_member_roles("acme", "owner1", roles=["owner", "billing"]))
    assert set(m.roles) == {"owner", "billing"}


def test_caps_only_change_on_the_last_owner_is_allowed():
    st = MemoryDataStore(); ms = MembershipStore(st); svc = _svc(st)
    _member(ms, "owner1", ["owner"])
    m = run(svc.update_member_roles("acme", "owner1", caps=["reports.read"]))  # roles untouched
    assert "owner" in m.roles and m.caps == ["reports.read"]


# ── suspend ───────────────────────────────────────────────────────────────────
def test_suspending_the_last_owner_is_refused():
    st = MemoryDataStore(); ms = MembershipStore(st); svc = _svc(st)
    _member(ms, "owner1", ["owner"])
    with pytest.raises(SoleOwnerError):
        run(svc.set_member_status("acme", "owner1", SUSPENDED))
    assert run(ms.membership("owner1", "acme")).status == ACTIVE   # untouched


def test_suspending_an_owner_with_a_co_owner_is_allowed():
    st = MemoryDataStore(); ms = MembershipStore(st); svc = _svc(st)
    _member(ms, "owner1", ["owner"])
    _member(ms, "owner2", ["owner"])
    assert run(svc.set_member_status("acme", "owner1", SUSPENDED)).status == SUSPENDED


def test_a_suspended_owner_no_longer_counts_so_the_second_cannot_be_suspended():
    """After one owner is suspended, the other is now the LAST ACTIVE owner and is protected."""
    st = MemoryDataStore(); ms = MembershipStore(st); svc = _svc(st)
    _member(ms, "owner1", ["owner"])
    _member(ms, "owner2", ["owner"])
    run(svc.set_member_status("acme", "owner1", SUSPENDED))
    with pytest.raises(SoleOwnerError):
        run(svc.set_member_status("acme", "owner2", SUSPENDED))
