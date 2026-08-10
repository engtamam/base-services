"""ONBOARDING_METHODS — the one fail-closed gate over how a new person may come to exist."""
from __future__ import annotations

import pytest

from app.core.onboarding import (INVITE_TOKEN, JOIN_REQUEST, OPEN, TEMP_PASSWORD,
                                 OnboardingMethodDisabled, OnboardingPolicy)
from app.ports.errors import ConfigError


def _p(csv, reg=True):
    return OnboardingPolicy(csv, registration_enabled=reg)


def test_an_explicit_list_is_the_whole_authority():
    p = _p("invite_token,join_request")
    assert p.enabled(INVITE_TOKEN) and p.enabled(JOIN_REQUEST)
    assert not p.enabled(OPEN) and not p.enabled(TEMP_PASSWORD)   # absent = disabled, fail-closed
    assert p.list_enabled() == ["invite_token", "join_request"]


def test_require_refuses_a_disabled_method():
    p = _p("invite_token")
    p.require(INVITE_TOKEN)                                       # enabled: no raise
    with pytest.raises(OnboardingMethodDisabled) as e:
        p.require(OPEN)
    assert e.value.method == OPEN


def test_empty_list_derives_from_legacy_knobs():
    # No explicit list: open follows REGISTRATION_ENABLED, admin-invite stays available, new methods off.
    on = _p("", reg=True)
    assert on.enabled(OPEN) and on.enabled(INVITE_TOKEN)
    assert not on.enabled(JOIN_REQUEST) and not on.enabled(TEMP_PASSWORD)
    off = _p("", reg=False)
    assert not off.enabled(OPEN) and off.enabled(INVITE_TOKEN)   # invite still on when open is off


def test_an_unknown_method_fails_at_construction_not_silently():
    with pytest.raises(ConfigError):
        _p("invite_token,teleport")


def test_the_public_endpoint_lists_enabled_methods():
    from starlette.testclient import TestClient
    from app.main import create_app
    c = TestClient(create_app())
    body = c.get("/api/v1/auth/onboarding-methods").json()
    assert "methods" in body and "open" in body["methods"]       # default: open + invite_token
