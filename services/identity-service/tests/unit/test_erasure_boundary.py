"""The DSAR <-> /auth/me boundary — 52.5.

Two paths can erase a person from a tenant:
  * DSAR fan-out          -> IdentityErasure.erase(tenant, subject)   (compliance orchestration)
  * self account delete   -> AuthService.delete_self(person_id, tenant)  (identity-local)

They are two entry points to ONE rule (revoke this membership; delete the Person on the last one).
The boundary this pins: whichever runs first, the other is a SAFE NO-OP — running both produces one
erasure, never a double-delete or an error. Both check existence first, so the second finds nothing
to do and says so.
"""
from __future__ import annotations

import asyncio

from app.adapters.datastore.memory import MemoryDataStore
from app.core.auth_service import AuthService
from app.core.erasure import IdentityErasure
from app.core.membership import Membership, MembershipStore, Person

_SALT = "s" * 32


def run(c):
    return asyncio.run(c)


class _Identifier:
    driver = "email"

    def display(self, value: str) -> str:
        return value.strip().casefold()

    def validate(self, value: str) -> None:
        pass


class _Users:
    async def get(self, uid):
        return None

    async def get_by_username(self, tenant, subject):
        return None

    async def delete(self, user):
        return None


class _Refresh:
    async def revoke_family(self, person_id):
        return 0


class _Hasher:
    def hash(self, s):
        return "argon2:" + s


def _world():
    store = MemoryDataStore()
    ms = MembershipStore(store)
    eraser = IdentityErasure(users=_Users(), refresh=_Refresh(), store=store, pii_salt=_SALT,
                             memberships=ms, identifier=_Identifier())
    svc = AuthService(users=_Users(), refresh=_Refresh(), hasher=_Hasher(), issuer=object(),
                      verifier=object(), mfa=object(), identifier=_Identifier(),
                      access_ttl=900, refresh_ttl=86400, memberships=ms)
    return store, ms, eraser, svc


def _person(ms, pid="p_1", identifier="ada@example.com", tenants=("acme",)):
    p = run(ms.create_person(Person(id=pid, identifier=identifier, password_hash="argon2:x")))
    for t in tenants:
        run(ms.grant(Membership(person_id=p.id, tenant=t, roles=["member"])))
    return p


def test_self_delete_then_dsar_is_a_safe_noop():
    _, ms, eraser, svc = _world()
    _person(ms)
    run(svc.delete_self("p_1", "acme"))
    assert run(ms.person("p_1")) is None
    # the DSAR arrives afterwards: nothing left to erase, and it says so — no error, no double-delete
    receipt = run(eraser.erase("acme", "ada@example.com"))
    assert receipt["already_absent"] is True
    assert "membership" not in receipt["erased"]


def test_dsar_then_self_delete_is_a_safe_noop():
    _, ms, eraser, svc = _world()
    _person(ms)
    receipt = run(eraser.erase("acme", "ada@example.com"))
    assert "membership" in receipt["erased"]
    assert run(ms.person("p_1")) is None
    # the account-delete arrives afterwards: the membership is already gone → NOT_FOUND, no error
    res = run(svc.delete_self("p_1", "acme"))
    assert res["deleted"] is False and res["reason"] == "NOT_FOUND"


def test_a_person_who_works_elsewhere_survives_both_paths():
    """Each path deletes only the acme relationship; beta is never touched by either."""
    _, ms, eraser, svc = _world()
    _person(ms, tenants=("acme", "beta"))
    run(svc.delete_self("p_1", "acme"))
    assert run(ms.person("p_1")) is not None          # kept — still works for beta
    assert run(ms.membership("p_1", "beta")) is not None
    # a later DSAR from acme retains the Person (beta relationship remains) and never touches beta
    receipt = run(eraser.erase("acme", "ada@example.com"))
    assert any(r["what"] == "person" for r in receipt["retained"])
    assert run(ms.person("p_1")) is not None
    assert run(ms.membership("p_1", "beta")) is not None
