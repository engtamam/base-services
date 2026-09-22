"""AuthService.change_password_person — the multi-tenant password change the Person model was missing.

Security edges: an absent person is None (route → 404, no oracle), an unclaimed (UNUSABLE) account
cannot be changed blindly, a wrong old password is refused, a weak new one raises, and a success
re-hashes and persists.
"""
from __future__ import annotations

import asyncio

import pytest

from app.adapters.datastore.memory import MemoryDataStore
from app.core.auth_service import AuthService, WeakPassword
from app.core.membership import UNUSABLE_PASSWORD, Membership, MembershipStore, Person


def run(c):
    return asyncio.run(c)


class _Hasher:
    def hash(self, s):
        return "h:" + s

    def verify(self, s, h):
        return h == "h:" + s


def _svc(store):
    return AuthService(
        users=object(), refresh=object(), hasher=_Hasher(), issuer=object(), verifier=object(),
        mfa=object(), identifier=object(), access_ttl=900, refresh_ttl=86400,
        memberships=MembershipStore(store), min_password_len=8)


def _person(ms, pid="p1", pw="old-good-secret"):
    return run(ms.create_person(Person(id=pid, identifier="o@a", password_hash="h:" + pw)))


def test_absent_person_returns_none_for_a_clean_404():
    st = MemoryDataStore()
    assert run(_svc(st).change_password_person("ghost", "x", "new-good-secret")) is None


def test_success_rehashes_and_persists():
    st = MemoryDataStore(); ms = MembershipStore(st); _person(ms)
    ok = run(_svc(st).change_password_person("p1", "old-good-secret", "new-good-secret"))
    assert ok is True
    assert run(ms.person("p1")).password_hash == "h:new-good-secret"


def test_wrong_old_password_is_false_and_changes_nothing():
    st = MemoryDataStore(); ms = MembershipStore(st); _person(ms)
    assert run(_svc(st).change_password_person("p1", "WRONG", "new-good-secret")) is False
    assert run(ms.person("p1")).password_hash == "h:old-good-secret"   # untouched


def test_unclaimed_person_cannot_be_changed():
    st = MemoryDataStore(); ms = MembershipStore(st)
    run(ms.create_person(Person(id="p2", identifier="invited@a", password_hash=UNUSABLE_PASSWORD)))
    # No usable password → any "old" is refused (False), never verified against the sentinel.
    assert run(_svc(st).change_password_person("p2", "anything", "new-good-secret")) is False
    assert run(ms.person("p2")).password_hash == UNUSABLE_PASSWORD


def test_weak_new_password_raises():
    st = MemoryDataStore(); ms = MembershipStore(st); _person(ms)
    with pytest.raises(WeakPassword):
        run(_svc(st).change_password_person("p1", "old-good-secret", "short"))
    assert run(ms.person("p1")).password_hash == "h:old-good-secret"   # not changed


def test_no_membership_store_returns_none():
    # multi-tenant off: no person path at all
    svc = AuthService(users=object(), refresh=object(), hasher=_Hasher(), issuer=object(),
                      verifier=object(), mfa=object(), identifier=object(), access_ttl=900,
                      refresh_ttl=86400, memberships=None, min_password_len=8)
    assert run(svc.change_password_person("p1", "x", "new-good-secret")) is None
