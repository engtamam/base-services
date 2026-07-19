"""Capacity to consent: is the person who agreed entitled to agree?

The bug this closes is silent by construction. A nine-year-old grants consent, nothing objects, and
the consent_proof — built to be the legal defence — faithfully records that we relied on consent
that was never lawful. Evidence AGAINST the platform, written by the platform.

So the tests are weighted towards what must be REFUSED, and towards the two defaults that decide
whether this helps or hurts: unknown age must not read as adult, and a platform that has not opted
in must behave exactly as it did before.
"""
from __future__ import annotations

import asyncio

import pytest

from app.core.capacity import YEAR_SECONDS, CapacityPolicy, CapacityRefused
from app.core.models import GUARDIAN, LEGAL_REPRESENTATIVE, SELF

_NOW = 1_800_000_000.0


def _born(years_ago: float) -> float:
    return _NOW - years_ago * YEAR_SECONDS


def _policy(age=16, unknown="refuse"):
    return CapacityPolicy(age_of_majority=age, unknown_age=unknown)


# ── the default: off, and unchanged ───────────────────────────────────────────
def test_off_by_default_so_existing_platforms_are_untouched():
    # Every deployment before this phase implicitly asserted "we do not serve minors". Turning the
    # check on by default would break all of them on upgrade.
    assert CapacityPolicy().enforced is False
    assert CapacityPolicy().check(basis=SELF, consented_by="", born_at=0, now=_NOW) == 0.0


def test_with_the_check_off_an_unknown_age_is_accepted():
    assert _policy(age=0).check(basis=SELF, consented_by="", born_at=0, now=_NOW) == 0.0


# ── THE fix: unknown age must not read as adult ───────────────────────────────
def test_unknown_age_is_refused_when_a_threshold_is_set():
    """The whole bug in one test. An absent birth date is precisely the case the threshold was set
    to ask about, so it is the last thing that should be waved through."""
    with pytest.raises(CapacityRefused, match="unknown"):
        _policy().check(basis=SELF, consented_by="", born_at=0, now=_NOW)


def test_the_refusal_says_how_to_fix_it():
    # A refusal the caller cannot act on just becomes a ticket.
    with pytest.raises(CapacityRefused) as exc:
        _policy().check(basis=SELF, consented_by="", born_at=0, now=_NOW)
    assert "subject_born_at" in str(exc.value) and "guardian" in str(exc.value)


def test_a_platform_that_cannot_collect_birth_dates_can_opt_out_explicitly():
    # Opt-in, and a decision someone has to write down — not a silent default.
    assert _policy(unknown="allow").check(basis=SELF, consented_by="", born_at=0, now=_NOW) == 0.0


# ── minors ────────────────────────────────────────────────────────────────────
def test_a_minor_cannot_consent_for_themself():
    with pytest.raises(CapacityRefused, match="below the age of majority"):
        _policy().check(basis=SELF, consented_by="", born_at=_born(9), now=_NOW)


def test_a_guardian_may_consent_for_a_minor():
    lapses = _policy().check(basis=GUARDIAN, consented_by="sub_parent", born_at=_born(9),
                             now=_NOW)
    assert lapses > _NOW                       # and it is dated


def test_guardian_consent_lapses_at_majority_not_never():
    """Consent obtained from a parent when the subject was 9 must not still be relied on at 30."""
    born = _born(9)
    lapses = _policy(age=16).check(basis=GUARDIAN, consented_by="sub_parent", born_at=born,
                                   now=_NOW)
    assert lapses == pytest.approx(born + 16 * YEAR_SECONDS)
    assert lapses == pytest.approx(_NOW + 7 * YEAR_SECONDS, rel=1e-6)   # 7 years from now


def test_an_adult_consents_for_themself_with_no_expiry():
    assert _policy().check(basis=SELF, consented_by="", born_at=_born(30), now=_NOW) == 0.0


def test_guardian_consent_for_someone_already_adult_does_not_outlive_them():
    # They can speak for themself now, so a guardian claim must not be dated into the future.
    assert _policy().check(basis=GUARDIAN, consented_by="sub_p", born_at=_born(30),
                           now=_NOW) == 0.0


# ── delegation beyond children ────────────────────────────────────────────────
def test_the_same_mechanism_covers_a_legal_representative():
    """Naming this after minors would have needed a second mechanism for carers, attorneys and
    executors. Capacity covers them all."""
    assert _policy().check(basis=LEGAL_REPRESENTATIVE, consented_by="sub_attorney",
                           born_at=_born(80), now=_NOW) == 0.0


def test_delegated_consent_works_even_with_the_age_check_off():
    # A power of attorney does not depend on an age threshold.
    assert CapacityPolicy().check(basis=LEGAL_REPRESENTATIVE, consented_by="sub_att",
                                  born_at=0, now=_NOW) == 0.0


def test_someone_consenting_for_another_must_be_identified():
    # "A guardian agreed" with nobody named is an unfalsifiable claim, and the proof would record
    # nothing anyone could check.
    with pytest.raises(CapacityRefused, match="requires consented_by"):
        _policy().check(basis=GUARDIAN, consented_by="", born_at=_born(9), now=_NOW)


@pytest.mark.parametrize("bad", ["parent", "", "SELF", "adult"])
def test_an_unknown_basis_is_refused(bad):
    with pytest.raises(CapacityRefused, match="not one of"):
        _policy().check(basis=bad, consented_by="x", born_at=_born(30), now=_NOW)


def test_refusal_is_an_exception_not_a_boolean():
    """A boolean would let one call site forget to check it, and the failure would be silent in
    exactly the way this file exists to prevent."""
    assert issubclass(CapacityRefused, Exception)
