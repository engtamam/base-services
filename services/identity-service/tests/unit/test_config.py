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


def test_otp_template_default_is_a_valid_meta_template_name():
    # Meta WhatsApp template names are lowercase + digits + underscore ONLY — a dot ("auth.otp") is
    # rejected at registration, so OTP-over-WhatsApp could never go live. Guard the default.
    import re
    s = _make(INTERNAL_SERVICE_TOKEN="a" * 40, ENV="test")
    assert re.fullmatch(r"[a-z0-9_]+", s.OTP_TEMPLATE), s.OTP_TEMPLATE
