"""Erasure must not leave the login behind — and must not delete it too eagerly either.

The bug this pins: `IdentityErasure.erase()` deleted the `users` row and returned
`"retained": []`, while the Person record — identifier, password hash, MFA secret — survived
untouched. A DSAR reported complete erasure and left the most sensitive record in the platform in
place. A false "she was erased", in the one service whose docstring says it keeps nothing.

The correction is not "also delete the Person", because a Person is ONE HUMAN who may work for
several tenants and a DSAR is made BY one of them. Deleting outright would log that person out of
companies that never asked. So the rule has two halves, and both are tested here.
"""
from __future__ import annotations

import asyncio

import pytest

from app.adapters.datastore.memory import MemoryDataStore
from app.core.erasure import IdentityErasure
from app.core.membership import Membership, MembershipStore, Person

_SALT = "s" * 32


def run(c):
    return asyncio.run(c)


class _Identifier:
    """The LoginIdentifier port's normalising half — the same one auth uses."""

    driver = "email"

    def display(self, value: str) -> str:
        return value.strip().casefold()


class _Users:
    """UserRepository stand-in: the single-tenant side, already handled before this fix."""

    async def get_by_username(self, tenant, subject):
        return None

    async def delete(self, user):
        return None


class _Refresh:
    def __init__(self):
        self.revoked: list[str] = []

    async def revoke_family(self, person_id):
        self.revoked.append(person_id)
        return 1


def _world():
    store = MemoryDataStore()
    memberships = MembershipStore(store)
    refresh = _Refresh()
    eraser = IdentityErasure(users=_Users(), refresh=refresh, store=store, pii_salt=_SALT,
                             memberships=memberships, identifier=_Identifier())
    return store, memberships, refresh, eraser


def _person(memberships, identifier="Ada@Example.com", tenants=("acme",)):
    person = run(memberships.create_person(
        Person(id="p_1", identifier=identifier.strip().casefold(), password_hash="argon2:xyz",
               mfa_secret="TOTPSECRET")))
    for tenant in tenants:
        run(memberships.grant(Membership(person_id=person.id, tenant=tenant)))
    return person


def test_the_person_row_does_not_survive_a_dsar_when_it_was_their_only_tenant():
    """THE bug. identifier, password hash and MFA secret all went unerased while the receipt said
    nothing was kept."""
    store, memberships, _, eraser = _world()
    person = _person(memberships)

    ack = run(eraser.erase("acme", "Ada@Example.com"))

    assert run(memberships.person_by_identifier("ada@example.com")) is None
    assert "person" in ack["erased"]
    assert "identifier_index" in ack["erased"]
    assert ack["retained"] == []


def test_the_password_hash_and_mfa_secret_are_gone_from_the_store():
    """Not just unreachable by lookup — actually absent. A row that is only unindexed is still a
    row an attacker with store access can read."""
    store, memberships, _, eraser = _world()
    _person(memberships)
    run(eraser.erase("acme", "ada@example.com"))

    dumped = str(store._data if hasattr(store, "_data") else "")
    assert "argon2:xyz" not in dumped
    assert "TOTPSECRET" not in dumped


def test_refresh_tokens_are_revoked_when_the_person_goes():
    store, memberships, refresh, eraser = _world()
    person = _person(memberships)
    run(eraser.erase("acme", "ada@example.com"))
    assert person.id in refresh.revoked


def test_a_person_who_still_works_elsewhere_is_KEPT_and_the_receipt_says_so():
    """The half that makes this subtle. One human, two employers; one of them asks us to forget
    them. Deleting the login would log them out of the other company, which never asked."""
    store, memberships, _, eraser = _world()
    _person(memberships, tenants=("acme", "globex"))

    ack = run(eraser.erase("acme", "ada@example.com"))

    assert run(memberships.person_by_identifier("ada@example.com")) is not None
    assert "membership" in ack["erased"]          # the relationship acme controls IS ended
    assert "person" not in ack["erased"]
    assert len(ack["retained"]) == 1
    kept = ack["retained"][0]
    assert kept["what"] == "person"
    assert kept["basis"] == "other_controller_relationships"
    assert "never asked" in kept["why"]           # the reason is stated, not implied


def test_the_last_membership_ending_takes_the_person_with_it():
    """Retention is temporary and self-clearing: when the final door closes, the login goes."""
    store, memberships, _, eraser = _world()
    _person(memberships, tenants=("acme", "globex"))

    run(eraser.erase("acme", "ada@example.com"))
    assert run(memberships.person_by_identifier("ada@example.com")) is not None

    ack = run(eraser.erase("globex", "ada@example.com"))
    assert run(memberships.person_by_identifier("ada@example.com")) is None
    assert "person" in ack["erased"] and ack["retained"] == []


def test_the_identifier_is_normalised_before_lookup():
    """A DSAR quoting 'Ada@Example.com' must find the person stored as 'ada@example.com'. Matching
    raw would silently erase nobody while reporting success."""
    store, memberships, _, eraser = _world()
    _person(memberships, identifier="ada@example.com")

    run(eraser.erase("acme", "  Ada@EXAMPLE.com  "))
    assert run(memberships.person_by_identifier("ada@example.com")) is None


def test_erasing_an_unknown_subject_is_already_absent_not_an_error():
    """A DSAR retries, and the second pass must not fail."""
    store, memberships, _, eraser = _world()
    ack = run(eraser.erase("acme", "nobody@example.com"))
    assert ack["already_absent"] is True
    assert ack["erased"] == [] and ack["retained"] == []


def test_erasing_twice_is_idempotent():
    store, memberships, _, eraser = _world()
    _person(memberships)
    first = run(eraser.erase("acme", "ada@example.com"))
    second = run(eraser.erase("acme", "ada@example.com"))
    assert "person" in first["erased"]
    assert second["erased"] == [] and second["already_absent"] is True


def test_single_tenant_deployments_are_unaffected():
    """IDENTITY_MULTI_TENANT off means no Person rows exist. Saying nothing about them is correct
    rather than a silence."""
    eraser = IdentityErasure(users=_Users(), refresh=_Refresh(), store=MemoryDataStore(),
                             pii_salt=_SALT, memberships=None, identifier=None)
    ack = run(eraser.erase("acme", "ada@example.com"))
    assert ack["erased"] == [] and ack["retained"] == []
