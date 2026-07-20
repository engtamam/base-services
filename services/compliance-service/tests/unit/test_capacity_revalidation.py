"""Capacity is a fact about the subject's age, not a value somebody remembered to write down.

grant() folds a lapse date into expires_at, which covers every consent granted since the policy
existed — and covers NOTHING granted before it. Those rows carry expires_at=0, and no amount of
waiting changes a stored zero. check() read only granted/expires_at, so it never noticed; the
lapse sweeper opened with `if not expires_at: continue`, so the one category it existed for was the
one category it skipped.

Net effect before this: a guardian consent granted while the policy was off answered `allowed`
for ever, and nothing on the platform would ever revisit it.

The third fix here is smaller and older: the granted/expiry rule was written twice — once in a
model method nothing called, once inline in check() because it needed WHICH reason and a bool
cannot carry one. Two copies of the rule that decides whether processing is lawful, one of them
free to rot.
"""
from __future__ import annotations

import asyncio

import pytest

from app.core.capacity import CapacityPolicy
from app.core.models import Consent

_YEAR = 365.2425 * 86400
NOW = 1_700_000_000.0


def run(c):
    return asyncio.run(c)


def _born(years_ago: float) -> float:
    return NOW - years_ago * _YEAR


# ── 1. one rule, one place ─────────────────────────────────────────────────────
def test_status_at_names_the_reason_rather_than_returning_a_bool():
    """The reason is why the rule was duplicated: check() needed WHICH refusal, a bool could not
    carry it, so the inline copy was written and the model copy went dead."""
    c = Consent(tenant="acme", subject="s", purpose="p", granted=False)
    assert c.status_at(NOW) == "withdrawn"


def test_active_is_now_derived_from_the_same_rule():
    """Both forms exist, but only one implementation — so there is nothing left to drift."""
    c = Consent(tenant="acme", subject="s", purpose="p", granted=True)
    assert c.active(NOW) is True
    assert c.status_at(NOW) == "granted"


def test_expiry_still_wins():
    c = Consent(tenant="acme", subject="s", purpose="p", granted=True, expires_at=NOW - 1)
    assert c.status_at(NOW) == "expired"
    assert c.active(NOW) is False


# ── 2. capacity re-evaluated on READ ───────────────────────────────────────────
def test_a_delegated_consent_lapses_at_majority_even_with_NO_stored_expiry():
    """THE gap. A row granted before the policy existed carries expires_at=0; deriving the
    majority date on read is what finally catches it."""
    c = Consent(tenant="acme", subject="kid", purpose="p", granted=True,
                basis="guardian", expires_at=0.0)
    majority = NOW - 1                                    # they turned 18 yesterday
    assert c.status_at(NOW, majority) == "lapsed_at_majority"


def test_a_delegated_consent_before_majority_still_stands():
    c = Consent(tenant="acme", subject="kid", purpose="p", granted=True, basis="guardian")
    assert c.status_at(NOW, NOW + _YEAR) == "granted"


def test_a_SELF_consent_is_never_lapsed_by_majority():
    """An adult consenting for themself has no majority date. Applying one would expire consents
    nobody agreed to end."""
    c = Consent(tenant="acme", subject="s", purpose="p", granted=True, basis="self")
    assert c.status_at(NOW, NOW - 1) == "granted"


def test_an_UNEVALUATED_capacity_falls_back_to_the_stored_expiry():
    """None means 'we could not evaluate capacity' — no policy, or no birth date. It must not
    manufacture permission, and it must not manufacture a refusal either."""
    c = Consent(tenant="acme", subject="kid", purpose="p", granted=True, basis="guardian")
    assert c.status_at(NOW, None) == "granted"


# ── 3. the sweeper stops skipping the rows it exists for ───────────────────────
class _Clock:
    def now(self):
        return NOW


class _Bus:
    def __init__(self):
        self.published = []

    async def publish(self, event, payload):
        self.published.append((event, payload))


def _sweeper(store, capacity):
    from app.core.consent_sweeper import ConsentSweeper
    return ConsentSweeper(store=store, consents=None, clock=_Clock(), event_bus=_Bus(),
                          notice_seconds=0.0, max_per_run=100, capacity=capacity)


def _row(store, *, basis="guardian", born_years=20.0, expires_at=0.0):
    row = {"tenant": "acme", "subject": "kid@example.com", "purpose": "coaching",
           "basis": basis, "granted": True, "expires_at": expires_at,
           "subject_born_at": _born(born_years)}
    run(store.put("consent", "acme::kid@example.com::coaching", row))
    return row


def test_a_PRE_POLICY_row_gets_its_lapse_date_derived_and_written_back():
    """The record catches up with the policy instead of staying frozen at what was true when it
    was created."""
    from app.adapters.datastore.memory import MemoryDataStore

    store = MemoryDataStore()
    _row(store, born_years=20.0)                         # already an adult
    sw = _sweeper(store, CapacityPolicy(age_of_majority=18))

    out = run(sw.run())
    assert out["lapsed"] == 1
    saved = run(store.get("consent", "acme::kid@example.com::coaching"))
    assert saved["expires_at"] > 0, "the derived lapse date was not written back"


def test_a_pre_policy_row_for_a_MINOR_is_left_alone_until_they_grow_up():
    from app.adapters.datastore.memory import MemoryDataStore

    store = MemoryDataStore()
    _row(store, born_years=10.0)
    sw = _sweeper(store, CapacityPolicy(age_of_majority=18))
    out = run(sw.run())
    assert out["lapsed"] == 0


def test_an_unknown_birth_date_derives_NOTHING():
    """Guessing a lapse date would expire a consent nobody agreed to end — the same class of
    error as inventing one that never ends, pointed the other way."""
    from app.adapters.datastore.memory import MemoryDataStore

    store = MemoryDataStore()
    row = _row(store, born_years=20.0)
    row["subject_born_at"] = 0.0
    run(store.put("consent", "acme::kid@example.com::coaching", row))

    sw = _sweeper(store, CapacityPolicy(age_of_majority=18))
    assert run(sw.run())["lapsed"] == 0


def test_a_self_consent_is_not_swept():
    from app.adapters.datastore.memory import MemoryDataStore

    store = MemoryDataStore()
    _row(store, basis="self", born_years=20.0)
    sw = _sweeper(store, CapacityPolicy(age_of_majority=18))
    assert run(sw.run())["lapsed"] == 0


def test_with_the_policy_OFF_nothing_is_derived():
    """A platform that has not decided it serves minors must not have lapse dates invented for it."""
    from app.adapters.datastore.memory import MemoryDataStore

    store = MemoryDataStore()
    _row(store, born_years=20.0)
    sw = _sweeper(store, CapacityPolicy(age_of_majority=0))
    assert run(sw.run())["lapsed"] == 0


# ── 2b. the READ PATH itself ───────────────────────────────────────────────────
# A mutation exposed a hole in this file: disabling the majority derivation inside check() broke
# nothing, because every test above exercised the model or the sweeper and none exercised the hot
# path that actually answers "may I process this person's data".
def _consent_service(capacity):
    from app.adapters.datastore.memory import MemoryDataStore
    from app.core.consent_service import ConsentService
    from app.core.stores import ConsentStore

    return ConsentService(store=ConsentStore(MemoryDataStore()), clock=_Clock(), cache=None,
                          capacity=capacity)


def test_CHECK_refuses_a_lapsed_delegated_consent_with_no_stored_expiry():
    """The hot path, on a pre-policy row. Before this, check() read granted/expires_at only and
    answered `allowed` for ever."""
    svc = _consent_service(CapacityPolicy(age_of_majority=18))
    run(svc._store.put(Consent(tenant="acme", subject="kid", purpose="coaching", granted=True,
                               basis="guardian", expires_at=0.0,
                               subject_born_at=_born(20.0))))

    out = run(svc.check(tenant="acme", subject="kid", purpose="coaching"))
    assert out["allowed"] is False
    assert out["reason"] == "lapsed_at_majority"


def test_CHECK_still_allows_a_delegated_consent_before_majority():
    svc = _consent_service(CapacityPolicy(age_of_majority=18))
    run(svc._store.put(Consent(tenant="acme", subject="kid", purpose="coaching", granted=True,
                               basis="guardian", subject_born_at=_born(10.0))))
    assert run(svc.check(tenant="acme", subject="kid", purpose="coaching"))["allowed"] is True


def test_CHECK_is_unaffected_when_the_policy_is_off():
    """A platform that has not decided it serves minors keeps its previous behaviour exactly."""
    svc = _consent_service(CapacityPolicy(age_of_majority=0))
    run(svc._store.put(Consent(tenant="acme", subject="kid", purpose="coaching", granted=True,
                               basis="guardian", subject_born_at=_born(20.0))))
    assert run(svc.check(tenant="acme", subject="kid", purpose="coaching"))["allowed"] is True
