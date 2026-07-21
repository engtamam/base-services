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


def test_a_refresh_longer_than_the_rotation_window_is_refused(monkeypatch):
    """Past that point the two stop overlapping and a rotation leaves live replicas verifying with
    a credential the provider has already stopped accepting."""
    import pytest

    from app.config import Settings

    s = Settings(INTERNAL_SERVICE_TOKEN="t" * 40, ENV="test", SECRETS_KEK="k",
                 PROVIDER_REFRESH_SECONDS=600, SECRETS_ROTATION_WINDOW_SECONDS=300)
    with pytest.raises(ValueError, match="PROVIDER_REFRESH_SECONDS"):
        s.assert_verify_config_ok()
