"""Erasure-fan-out config: discovery is config, paths are per-target, and boot fails closed."""
from __future__ import annotations

import pytest

from app.config import Settings

_OK = {"INTERNAL_SERVICE_TOKEN": "x" * 40}


def test_targets_parse_to_name_url():
    s = Settings(PRIVACY_TARGETS="identity-service=http://identity:8080, acme-crm=https://crm.acme/ ",
                 **_OK)
    assert s.privacy_targets == {"identity-service": "http://identity:8080",
                                 "acme-crm": "https://crm.acme"}   # trailing slash trimmed


@pytest.mark.parametrize("bad", ["identity-service", "identity-service=", "=http://x",
                                 "identity-service=ftp://x"])
def test_malformed_target_fails_at_boot_not_at_erasure(bad):
    s = Settings(PRIVACY_TARGETS=bad, **_OK)
    with pytest.raises(ValueError):
        s.privacy_targets


def test_erase_path_defaults_and_per_target_override():
    s = Settings(PRIVACY_ERASE_PATH="/internal/privacy/erase",
                 PRIVACY_ERASE_PATHS="acme-crm=/api/gdpr/forget", **_OK)
    # our convention is only a DEFAULT — another platform's path is its own
    assert s.erase_path_for("identity-service") == "/internal/privacy/erase"
    assert s.erase_path_for("acme-crm") == "/api/gdpr/forget"


def test_malformed_path_override_is_refused():
    s = Settings(PRIVACY_ERASE_PATHS="acme-crm=no-leading-slash", **_OK)
    with pytest.raises(ValueError):
        s.privacy_erase_paths


def test_dev_defaults_boot_with_zero_infra():
    Settings(ENV="test", **_OK).assert_privacy_config_ok()


def test_production_refuses_a_dispatcher_that_can_reach_nothing():
    # null transport in prod: DSARs would be accepted and never dispatched.
    with pytest.raises(ValueError, match="never dispatched"):
        Settings(ENV="production", PRIVACY_TRANSPORT_DRIVER="null", **_OK).assert_privacy_config_ok()
    # http transport but no targets: every DSAR hangs forever.
    with pytest.raises(ValueError, match="PRIVACY_TARGETS is empty"):
        Settings(ENV="production", PRIVACY_TRANSPORT_DRIVER="http", PRIVACY_TARGETS="",
                 **_OK).assert_privacy_config_ok()


def test_production_happy_config_passes():
    Settings(ENV="production", PRIVACY_TRANSPORT_DRIVER="http",
             PRIVACY_TARGETS="identity-service=http://identity:8080",
             **_OK).assert_privacy_config_ok()


def test_third_party_targets_get_their_own_credential_not_ours():
    # Sending INTERNAL_SERVICE_TOKEN to someone else's host would leak a credential that unlocks
    # every one of our services. A third party is named a SECRET KEY, resolved via the secrets port.
    s = Settings(PRIVACY_TARGET_SECRETS="acme-crm=ACME_CRM_ERASE_TOKEN", **_OK)
    assert s.secret_key_for("acme-crm") == "ACME_CRM_ERASE_TOKEN"
    assert s.secret_key_for("identity-service") is None   # ours: internal token


@pytest.mark.parametrize("bad", ["acme-crm", "acme-crm=", "=KEY",
                                 "acme-crm=/looks/like/a/path",
                                 "acme-crm=https://not-a-secret-name"])
def test_target_secret_must_be_a_name_never_an_inline_value(bad):
    with pytest.raises(ValueError):
        Settings(PRIVACY_TARGET_SECRETS=bad, **_OK).privacy_target_secrets
