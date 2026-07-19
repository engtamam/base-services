"""An empty PII_SALT must stop the service STARTING — the compliance-service half.

Same defect and same fix as identity-service, and it matters more here: this is the service that
ORCHESTRATES erasure for the whole platform. An unsalted compliance-service accepts DSARs, fans
them out, and fails on its own leg at the moment it tries to pseudonymise — after telling the
subject their request was accepted.

The guard lives in assert_privacy_config_ok() because that is already the boot check for "can
this service actually finish a DSAR", which is exactly the question an empty salt answers no to.
"""
from __future__ import annotations

import pytest

from app.config import Settings


def _settings(**kw):
    base = dict(ENV="production", PII_SALT="s" * 32,
                PRIVACY_TRANSPORT_DRIVER="http", PRIVACY_TARGETS="identity=http://identity:8000")
    base.update(kw)
    return Settings(**base)


def test_production_refuses_to_start_with_an_empty_salt():
    with pytest.raises(ValueError) as e:
        _settings(PII_SALT="").assert_privacy_config_ok()
    assert "PII_SALT" in str(e.value)


def test_the_message_explains_the_consequence_not_just_the_missing_variable():
    with pytest.raises(ValueError) as e:
        _settings(PII_SALT="").assert_privacy_config_ok()
    msg = str(e.value)
    assert "FROZEN" in msg
    assert "reversible" in msg


def test_a_configured_salt_starts_normally():
    _settings().assert_privacy_config_ok()


def test_development_is_not_blocked():
    _settings(ENV="development", PII_SALT="").assert_privacy_config_ok()


def test_the_existing_transport_guards_still_fire():
    """The new check sits alongside older ones. Added rather than replaced — this fails if the
    salt check ever short-circuits the dispatcher checks that were already there."""
    with pytest.raises(ValueError) as e:
        _settings(PRIVACY_TRANSPORT_DRIVER="null").assert_privacy_config_ok()
    assert "PRIVACY_TRANSPORT_DRIVER" in str(e.value)

    with pytest.raises(ValueError) as e:
        _settings(PRIVACY_TARGETS="").assert_privacy_config_ok()
    assert "PRIVACY_TARGETS" in str(e.value)
