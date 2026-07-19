"""`Matching` and `matching` were two different consents.

purpose was raw free text used directly as a store key, so a caller asking with the wrong casing
got `no_consent_record` — the IDENTICAL answer given for a person who genuinely never consented.
The failure direction is a default-deny, so this was never an unlawful-processing hole. It was
undiagnosable, which is its own kind of bad: nobody could tell "they said no" from "you typed it
differently", and the caller's only symptom was a refusal that looked real.

The other half of this file is the part that could go wrong in production: rows written before
normalisation must keep working, and must not end up with a duplicate twin.
"""
from __future__ import annotations

import asyncio

import pytest

from app.adapters.datastore.memory import MemoryDataStore
from app.core.models import Consent
from app.core.stores import ConsentStore


def run(c):
    return asyncio.run(c)


def _store():
    return ConsentStore(MemoryDataStore())


def _consent(purpose, granted=True):
    return Consent(tenant="acme", subject="ada@example.com", purpose=purpose, granted=granted)


# ── the rule ───────────────────────────────────────────────────────────────────
@pytest.mark.parametrize("written,asked", [
    ("Matching", "matching"),
    ("matching", "MATCHING"),
    ("Matching", "  matching  "),
    ("  Matching ", "matching"),
])
def test_casing_and_whitespace_no_longer_split_a_consent(written, asked):
    s = _store()
    run(s.put(_consent(written)))
    assert run(s.get("acme", "ada@example.com", asked)) is not None


def test_genuinely_different_purposes_stay_different():
    """The check that stops this being over-eager. Merging two purposes would be far worse than
    failing to match them — it would apply one consent to processing nobody agreed to."""
    s = _store()
    run(s.put(_consent("marketing")))
    assert run(s.get("acme", "ada@example.com", "analytics")) is None


def test_internal_punctuation_is_NOT_collapsed():
    """`data-sharing` and `data sharing` are plausibly deliberate, distinct choices. Only case and
    surrounding whitespace are normalised."""
    s = _store()
    run(s.put(_consent("data-sharing")))
    assert run(s.get("acme", "ada@example.com", "data sharing")) is None


def test_tenants_still_do_not_share_consents():
    s = _store()
    run(s.put(_consent("matching")))
    assert run(s.get("globex", "ada@example.com", "Matching")) is None


# ── the migration, which is where the risk is ──────────────────────────────────
def test_a_legacy_row_is_reachable_by_its_ORIGINAL_spelling():
    """The fallback keeps existing callers working the moment this deploys."""
    s = _store()
    run(s._store.put("consent", "acme::ada@example.com::Matching", _consent("Matching").to_dict()))
    assert run(s.get("acme", "ada@example.com", "Matching")) is not None


def test_a_legacy_row_is_NOT_reachable_by_the_normalised_spelling_until_migrated():
    """The honest limit of the fallback, and the reason the migration exists.

    casefold has no inverse: asking `matching` cannot discover a row stored as `Matching`. So for
    exactly the caller this change was meant to help, the drift survives until the row is
    rewritten. A test found this — the original design shipped only the fallback and would have
    left the defect in place while looking fixed.
    """
    s = _store()
    run(s._store.put("consent", "acme::ada@example.com::Matching", _consent("Matching").to_dict()))
    assert run(s.get("acme", "ada@example.com", "matching")) is None


def test_the_migration_makes_it_reachable():
    s = _store()
    run(s._store.put("consent", "acme::ada@example.com::Matching", _consent("Matching").to_dict()))

    out = run(s.migrate_purpose_keys())
    assert out["migrated"] == 1
    assert run(s.get("acme", "ada@example.com", "matching")) is not None
    assert await_get(s, "acme::ada@example.com::Matching") is None


def test_the_migration_is_idempotent():
    """An operator who runs it twice, or a retried job, must not thrash the records."""
    s = _store()
    run(s._store.put("consent", "acme::ada@example.com::Matching", _consent("Matching").to_dict()))
    run(s.migrate_purpose_keys())
    assert run(s.migrate_purpose_keys())["migrated"] == 0


def test_a_migration_conflict_lets_DENY_win_and_is_reported():
    """Both spellings existed. Merging silently would be the worst option; so would picking the
    permissive one. Deny beats allow because a wrong denial costs a refused request and a wrong
    allow costs processing someone objected to — and the conflict is named either way."""
    s = _store()
    run(s._store.put("consent", "acme::ada@example.com::Matching",
                     _consent("Matching", granted=True).to_dict()))
    run(s._store.put("consent", "acme::ada@example.com::matching",
                     _consent("matching", granted=False).to_dict()))

    out = run(s.migrate_purpose_keys())
    assert len(out["conflicts"]) == 1
    assert out["conflicts"][0]["purpose"] == "Matching"
    survivor = run(s.get("acme", "ada@example.com", "matching"))
    assert survivor.granted is False, "a grant overrode a withdrawal"


def test_rewriting_under_the_SAME_spelling_retires_the_legacy_row():
    """put() can only retire the twin whose spelling it was handed. Given `Matching`, it writes
    the normalised key and deletes the raw one — the lazy half of the migration."""
    s = _store()
    run(s._store.put("consent", "acme::ada@example.com::Matching", _consent("Matching").to_dict()))

    run(s.put(_consent("Matching", granted=False)))

    assert await_get(s, "acme::ada@example.com::Matching") is None, "legacy twin survived"
    assert await_get(s, "acme::ada@example.com::matching") is not None


def test_a_legacy_twin_is_SHADOWED_even_before_it_is_deleted():
    """A test corrected me here. Writing `matching` cannot retire a `Matching` row — put() never
    sees the other spelling — so for a while both rows exist on disk.

    That is safe, and this pins why: get() tries the NORMALISED key first, so the live row always
    answers and the legacy one can never win. It is an orphan awaiting the migration, not a
    second source of truth. Had the lookup order been the other way round, this would be a
    withdrawal that stops taking effect.
    """
    s = _store()
    run(s._store.put("consent", "acme::ada@example.com::Matching",
                     _consent("Matching", granted=True).to_dict()))

    run(s.put(_consent("matching", granted=False)))

    assert await_get(s, "acme::ada@example.com::Matching") is not None   # still on disk
    for spelling in ("matching", "Matching", "  MATCHING "):
        found = run(s.get("acme", "ada@example.com", spelling))
        assert found.granted is False, f"the stale grant answered for {spelling!r}"


def test_a_withdrawal_under_new_casing_beats_a_legacy_grant():
    """The consequence that matters. If the legacy row survived, a withdrawal recorded as
    `matching` could leave `Matching` still answering allowed — processing after a withdrawal,
    which is the violation this service exists to prevent."""
    s = _store()
    run(s._store.put("consent", "acme::ada@example.com::Matching", _consent("Matching").to_dict()))

    run(s.put(_consent("matching", granted=False)))

    found = run(s.get("acme", "ada@example.com", "Matching"))
    assert found is not None
    assert found.granted is False


def await_get(store: ConsentStore, key: str):
    return run(store._store.get("consent", key))
