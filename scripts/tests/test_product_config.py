"""The frozen-identifier guard, and the two ways it has already failed silently.

Both failures were the same shape: the check ran, printed nothing alarming, exited 0, and
protected nothing. Neither was caught by a passing gate — a gate that cannot fail is decoration.

  1. `check_frozen` compared product.json against `git show HEAD:config/product.json`. That file
     is never committed (same rule as .env), so git returned nothing and the check SKIPPED on
     every run since it was written. Fixed with a committed lock file.

  2. `_commit_allows_migration` was `"MIGRATION:" in body` — a substring search over the whole
     commit message. Any commit whose body mentioned the string anywhere granted itself the
     exemption, INCLUDING the commit that documented the rule. On that commit, changing
     `signatureDomain` — which invalidates every signature ever made, forever — passed with
     exit 0.

The tests below are written against the second failure specifically, because the fix is a
parser and a parser is exactly the thing that quietly accepts more than it meant to.
"""
from __future__ import annotations

import json
import pathlib
import sys

import pytest

ROOT = pathlib.Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "scripts"))

import check_product_config as cpc  # noqa: E402


@pytest.fixture(autouse=True)
def _reset():
    cpc._errors.clear()
    cpc._notes.clear()
    yield
    cpc._errors.clear()
    cpc._notes.clear()


def _with_body(monkeypatch, body: str) -> None:
    monkeypatch.setattr(cpc, "_git", lambda *a, **k: body)


# ── the exemption must be a directive, not a mention ────────────────────────
@pytest.mark.parametrize("body", [
    # THE REAL ONE. This is the body of the commit that introduced the lock file. Under the old
    # substring check it disarmed the very guard it was adding.
    "Make product identity real config: wire it, freeze it, gate it\n\n"
    "Both require a MIGRATION: line in the commit body.",
    "fix\n\n    MIGRATION: indented, so this is quoted prose, not a directive",
    "fix\n\nsee MIGRATION: rotate the key   (mid-sentence)",
    "fix\n\nMIGRATION:",                    # bare marker, no reason given
    "fix\n\nMIGRATION: x",                  # a reason too short to be one
    "fix\n\nno marker at all",
    "",
])
def test_a_mention_of_the_marker_does_not_grant_the_exemption(monkeypatch, body):
    _with_body(monkeypatch, body)
    assert cpc._commit_allows_migration() is False, (
        "this commit body would silently permit changing signatureDomain")


@pytest.mark.parametrize("body", [
    "MIGRATION: rotate jwtIssuer, 30-day dual-issuer window",          # subject line
    "chore: rotate\n\nMIGRATION: signatureDomain -> .v2; v1 keeps verifying for 90 days",
])
def test_a_real_directive_grants_the_exemption(monkeypatch, body):
    _with_body(monkeypatch, body)
    assert cpc._commit_allows_migration() is True


# ── the freeze itself ───────────────────────────────────────────────────────
def _cfg(**identifiers) -> dict:
    base = {k: "" for k in cpc.FROZEN}
    base.update(identifiers)
    return {"identifiers": base}


def test_drifting_from_the_lock_fails_without_a_directive(monkeypatch, tmp_path):
    lock = tmp_path / "lock.json"
    lock.write_text(json.dumps({k: "" for k in cpc.FROZEN}), encoding="utf-8")
    monkeypatch.setattr(cpc, "LOCK", lock)
    _with_body(monkeypatch, "a commit that says nothing about migrating")

    cpc.check_frozen(_cfg(signatureDomain="acme.sig.v1"))
    assert cpc._errors, "signatureDomain changed and the gate said nothing"
    assert "signatureDomain" in "\n".join(cpc._errors)


def test_matching_the_lock_passes(monkeypatch, tmp_path):
    values = {k: "" for k in cpc.FROZEN} | {"jwtIssuer": "acme"}
    lock = tmp_path / "lock.json"
    lock.write_text(json.dumps(values), encoding="utf-8")
    monkeypatch.setattr(cpc, "LOCK", lock)
    _with_body(monkeypatch, "an ordinary commit")

    cpc.check_frozen(_cfg(jwtIssuer="acme"))
    assert not cpc._errors


def test_a_missing_lock_is_a_failure_not_a_skip(monkeypatch, tmp_path):
    """The original bug: no baseline was read as 'nothing to check'."""
    monkeypatch.setattr(cpc, "LOCK", tmp_path / "absent.json")
    _with_body(monkeypatch, "an ordinary commit")

    cpc.check_frozen(_cfg(jwtIssuer="acme"))
    assert cpc._errors, "a missing lock left the frozen identifiers unguarded"


def test_rollout_salt_is_frozen():
    """Change it and every user re-buckets out of their feature-flag group, silently."""
    assert "rolloutSalt" in cpc.FROZEN


def test_the_shipped_lock_covers_every_frozen_identifier():
    lock = json.loads(cpc.LOCK.read_text(encoding="utf-8"))
    missing = [k for k in cpc.FROZEN if k not in lock]
    assert not missing, f"the lock does not pin {missing} — they are frozen in name only"
