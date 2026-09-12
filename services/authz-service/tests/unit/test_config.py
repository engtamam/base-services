"""Config is fail-fast and rejects weak/placeholder secrets."""
from __future__ import annotations

import pytest
from pydantic import ValidationError

from app.config import Settings


def _make(**overrides):
    # _env_file=None so a stray .env never interferes with the test.
    return Settings(_env_file=None, **overrides)


def test_missing_internal_token_fails_fast():
    with pytest.raises(ValidationError):
        _make(INTERNAL_SERVICE_TOKEN=None)  # type: ignore[arg-type]


def test_short_internal_token_rejected():
    with pytest.raises(ValidationError):
        _make(INTERNAL_SERVICE_TOKEN="tooshort")


def test_placeholder_internal_token_rejected():
    with pytest.raises(ValidationError):
        _make(INTERNAL_SERVICE_TOKEN="PLACEHOLDER_CHANGE_ME_MIN_32_CHARS____")


def test_valid_config_ok():
    s = _make(INTERNAL_SERVICE_TOKEN="a" * 40, ENV="test")
    assert s.is_test is True
    assert s.is_production is False


# ── authz caps wall boot guard (authz-r2-03) ──────────────────────────────────
def _prod(**overrides):
    return _make(INTERNAL_SERVICE_TOKEN="a" * 40, ENV="production", PII_SALT="s" * 32, **overrides)


def test_caps_guard_passes_with_default_denylist():
    _prod().assert_authz_caps_config_ok()  # defaults carry platform. + authz.manage


def test_caps_guard_refuses_empty_prefix_wall_in_prod():
    with pytest.raises(ValueError, match="platform."):
        _prod(AUTHZ_TENANT_DENIED_CAP_PREFIXES="").assert_authz_caps_config_ok()


def test_caps_guard_refuses_missing_authz_manage_deny_in_prod():
    with pytest.raises(ValueError, match="authz.manage"):
        _prod(AUTHZ_TENANT_DENIED_CAPS="").assert_authz_caps_config_ok()


def test_caps_guard_honours_explicit_override():
    _prod(AUTHZ_TENANT_DENIED_CAP_PREFIXES="", AUTHZ_TENANT_DENIED_CAPS="",
          AUTHZ_ALLOW_UNSAFE_TENANT_CAPS=True).assert_authz_caps_config_ok()


def test_caps_guard_is_noop_outside_production():
    _make(INTERNAL_SERVICE_TOKEN="a" * 40, ENV="test",
          AUTHZ_TENANT_DENIED_CAP_PREFIXES="").assert_authz_caps_config_ok()
