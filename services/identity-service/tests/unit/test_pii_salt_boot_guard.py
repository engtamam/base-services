"""An empty PII_SALT must stop the service STARTING, not one request at 3am.

core/pii.py already refuses an empty salt. That refusal is correct and it is also too late: the
only caller is IdentityErasure via subject_ref(), so the service boots, serves for months, and
raises the first time a real DSAR arrives — surfacing as a 500 on /internal/privacy/erase while
someone is exercising a legal right.

audit-service settled this argument already (tests/unit/test_pii_policy.py: it "must refuse to
START, not fail one request at 3am long after it was deployed"). This is the same rule applied to
the service whose own docstring says it keeps nothing.
"""
from __future__ import annotations

import pytest

from app.config import Settings


def _settings(**kw):
    base = dict(ENV="production", PII_SALT="s" * 32)
    base.update(kw)
    return Settings(**base)


def test_production_refuses_to_start_with_an_empty_salt():
    """THE guard."""
    with pytest.raises(ValueError) as e:
        _settings(PII_SALT="").assert_privacy_config_ok()
    assert "PII_SALT" in str(e.value)


def test_the_message_says_what_to_set_and_warns_that_it_is_frozen():
    """An operator hitting this at an interrupted deploy needs the fix AND the warning: choosing
    a fresh salt later re-maps every pseudonym and orphans the history."""
    with pytest.raises(ValueError) as e:
        _settings(PII_SALT="").assert_privacy_config_ok()
    msg = str(e.value)
    assert "FROZEN" in msg
    assert "reversible" in msg          # says WHY, not just that a variable is missing


def test_a_configured_salt_starts_normally():
    _settings().assert_privacy_config_ok()


def test_development_is_not_blocked():
    """Local runs and tests must not need the production secret — the guard is about what is
    SERVED, not about making the service unrunnable."""
    _settings(ENV="development", PII_SALT="").assert_privacy_config_ok()
    _settings(ENV="test", PII_SALT="").assert_privacy_config_ok()


def test_the_guard_is_reachable_from_the_erasure_path_it_protects():
    """Pins the connection rather than trusting it: the value the guard checks is the value
    erasure actually hashes with. If someone renames one, this fails instead of the guard
    silently protecting nothing."""
    from app.core.pii import subject_ref

    with pytest.raises(ValueError):
        subject_ref("", "acme", "ada@example.com")        # what a 500 would have come from
    assert subject_ref("s" * 32, "acme", "ada@example.com")
