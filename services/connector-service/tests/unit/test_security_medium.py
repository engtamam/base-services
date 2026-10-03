"""Security MEDIUM remediation: production SSRF/seal-key boot guards (connector-005, connector-013)."""
from __future__ import annotations

import pytest


def _settings(**over):
    from app.config import Settings
    base = dict(_env_file=None, INTERNAL_SERVICE_TOKEN="z" * 40, PII_SALT="s" * 16)
    base.update(over)
    return Settings(**base)


# ── connector-005: SSRF relaxations are refused in production ─────────────────────
def test_private_ips_refused_in_production():
    with pytest.raises(ValueError):
        _settings(ENV="production", INSTANCE_ALLOW_PRIVATE_IPS=True).assert_instance_ssrf_ok()


def test_cleartext_refused_in_production():
    with pytest.raises(ValueError):
        _settings(ENV="production", INSTANCE_REQUIRE_HTTPS=False).assert_instance_ssrf_ok()


def test_ssrf_defaults_fine_in_production():
    _settings(ENV="production", CREDENTIAL_SEAL_KEY="k" * 32).assert_instance_ssrf_ok()


def test_ssrf_relaxations_fine_outside_production():
    _settings(ENV="test", INSTANCE_ALLOW_PRIVATE_IPS=True,
              INSTANCE_REQUIRE_HTTPS=False).assert_instance_ssrf_ok()


# ── connector-013: credential seal key must be explicit in production ─────────────
def test_seal_key_required_in_production():
    with pytest.raises(ValueError):
        _settings(ENV="production", CREDENTIAL_SEAL_KEY="").assert_seal_key_ok()


def test_seal_key_present_is_fine():
    _settings(ENV="production", CREDENTIAL_SEAL_KEY="k" * 32).assert_seal_key_ok()


def test_seal_key_fallback_fine_in_dev():
    _settings(ENV="test").assert_seal_key_ok()
