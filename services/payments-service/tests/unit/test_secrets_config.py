"""L3 — boot guard for the sealing keys.

An empty SECRETS_KEK (or BACKUP_KEK) used to surface only deep on the first credential op, in
production, at whatever hour the first shop wired its gateway. assert_secrets_config_ok moves that
failure to boot. It applies only in production, exactly like the PII / audit / consent guards.
"""
from __future__ import annotations

import pytest

from app.config import Settings

_TOKEN = "s" * 40


def _make(**overrides) -> Settings:
    base = {"_env_file": None, "INTERNAL_SERVICE_TOKEN": _TOKEN}
    base.update(overrides)
    return Settings(**base)  # type: ignore[arg-type]


def test_empty_secrets_kek_refused_in_production():
    s = _make(ENV="production", SECRETS_KEK="", BACKUP_KEK="k")
    with pytest.raises(ValueError, match="SECRETS_KEK"):
        s.assert_secrets_config_ok()


def test_empty_backup_kek_refused_in_production():
    s = _make(ENV="production", SECRETS_KEK="k", BACKUP_KEK="")
    with pytest.raises(ValueError, match="BACKUP_KEK"):
        s.assert_secrets_config_ok()


def test_both_keys_set_in_production_is_ok():
    s = _make(ENV="production", SECRETS_KEK="k1", BACKUP_KEK="k2")
    s.assert_secrets_config_ok()  # must not raise


def test_empty_keys_tolerated_outside_production():
    # dev/test may run without the sealing keys (fail-closed at the op, not at boot).
    _make(ENV="test", SECRETS_KEK="", BACKUP_KEK="").assert_secrets_config_ok()
    _make(ENV="development", SECRETS_KEK="", BACKUP_KEK="").assert_secrets_config_ok()
