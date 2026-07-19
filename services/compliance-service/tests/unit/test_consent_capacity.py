"""Consent grant under a capacity policy — what gets STORED, and what must not be.

test_capacity.py pins the rule. This pins the consequences: a refusal must leave nothing behind,
and the proof must record whose consent it actually was.
"""
from __future__ import annotations

import asyncio

import pytest

from app.adapters.datastore.memory import MemoryDataStore
from app.core.capacity import YEAR_SECONDS, CapacityPolicy, CapacityRefused
from app.core.consent_service import ConsentService
from app.core.models import GUARDIAN, SELF
from app.core.stores import ConsentStore

_NOW = 1_800_000_000.0


def run(c):
    return asyncio.run(c)


class _Clock:
    def now(self):
        return _NOW


def _svc(age=16, unknown="refuse"):
    store = ConsentStore(MemoryDataStore())
    return ConsentService(store=store, clock=_Clock(),
                          capacity=CapacityPolicy(age_of_majority=age, unknown_age=unknown)), store


def _born(years):
    return _NOW - years * YEAR_SECONDS


def test_a_refused_grant_writes_nothing_at_all():
    """A proof recording a consent we refused to accept would be the same lie in the other
    direction — so the capacity check runs before anything is stored."""
    svc, store = _svc()
    with pytest.raises(CapacityRefused):
        run(svc.grant(tenant="acme", subject="u_9", purpose="marketing", subject_born_at=_born(9)))

    assert run(store.get("acme", "u_9", "marketing")) is None
    history = run(store.proofs("acme", "u_9"))
    assert history == [] or len(history) == 0


def test_a_guardian_grant_is_stored_with_who_agreed():
    svc, store = _svc()
    c = run(svc.grant(tenant="acme", subject="u_9", purpose="marketing", basis=GUARDIAN,
                      consented_by="sub_parent", relation="parent", subject_born_at=_born(9)))
    assert (c.basis, c.consented_by, c.relation) == (GUARDIAN, "sub_parent", "parent")
    assert c.granted is True


def test_the_proof_records_the_basis_not_just_the_operator():
    """`actor` is who typed it in. `consented_by` is whose consent it legally is. A regulator asks
    the second question, and it was the one we could not answer."""
    svc, store = _svc()
    run(svc.grant(tenant="acme", subject="u_9", purpose="marketing", basis=GUARDIAN,
                  consented_by="sub_parent", relation="parent", subject_born_at=_born(9),
                  actor="sub_admin"))
    proof = run(store.proofs("acme", "u_9"))[0]
    proof = proof if isinstance(proof, dict) else proof.to_dict()
    assert proof["actor"] == "sub_admin"          # who operated the system
    assert proof["consented_by"] == "sub_parent"  # whose consent it was
    assert proof["basis"] == GUARDIAN


def test_guardian_consent_is_stored_with_an_expiry_at_majority():
    svc, _ = _svc(age=16)
    c = run(svc.grant(tenant="acme", subject="u_9", purpose="marketing", basis=GUARDIAN,
                      consented_by="sub_parent", subject_born_at=_born(9)))
    assert c.expires_at == pytest.approx(_born(9) + 16 * YEAR_SECONDS)
    assert c.active(_NOW) is True
    assert c.active(c.expires_at + 1) is False     # lapses, rather than running for ever


def test_a_shorter_caller_expiry_is_not_silently_extended():
    # The capacity rule may only shorten consent, never lengthen it.
    svc, _ = _svc(age=16)
    soon = _NOW + 86400
    c = run(svc.grant(tenant="acme", subject="u_9", purpose="marketing", basis=GUARDIAN,
                      consented_by="sub_parent", subject_born_at=_born(9), expires_at=soon))
    assert c.expires_at == soon


def test_an_ordinary_adult_grant_is_unchanged():
    svc, _ = _svc()
    c = run(svc.grant(tenant="acme", subject="u_30", purpose="marketing",
                      subject_born_at=_born(30)))
    assert c.basis == SELF and c.consented_by == "" and c.expires_at == 0.0


def test_with_the_policy_off_nothing_changes_for_existing_callers():
    """The upgrade path. A deployment that never sets an age of majority must behave exactly as it
    did before this phase existed."""
    svc, _ = _svc(age=0)
    c = run(svc.grant(tenant="acme", subject="u_x", purpose="marketing"))
    assert c.granted is True and c.basis == SELF and c.expires_at == 0.0
