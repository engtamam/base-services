"""Security MEDIUM remediation: destructive /internal route boot guard (payments-008)."""
from __future__ import annotations

import pytest


def _settings(**over):
    from app.config import Settings
    base = dict(_env_file=None, INTERNAL_SERVICE_TOKEN="z" * 40, PII_SALT="s" * 16,
                SECRETS_KEK="k" * 16)
    base.update(over)
    return Settings(**base)


def test_boot_guard_refuses_prod_with_merchant_credentials_default_open():
    with pytest.raises(ValueError):
        _settings(ENV="production", INTERNAL_ROUTE_POLICY_JSON="").assert_internal_policy_ok()


def test_boot_guard_passes_when_declared():
    pol = ('{"/internal/payments/merchant-credentials": ["operations"], '
           '"/internal/payments/sub-merchants/onboard": ["operations"], '
           '"/internal/payments/sub-merchants/destination": ["operations"], '
           '"/internal/privacy/erase": ["compliance-service"], '
           '"/internal/backup/import": ["backup-service"]}')
    _settings(ENV="production", INTERNAL_ROUTE_POLICY_JSON=pol).assert_internal_policy_ok()


def test_boot_guard_noop_outside_production():
    _settings(ENV="test", INTERNAL_ROUTE_POLICY_JSON="").assert_internal_policy_ok()
