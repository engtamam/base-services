"""phone identifier — the reporter's #1 requirement: normalization IS identity, and uniqueness
survives it. 0501234567, +966501234567 and 966501234567 are ONE person → ONE canonical key."""
from __future__ import annotations

import pytest

from app.adapters.login_identifier.phone import PhoneIdentifier
from app.ports.errors import ValidationError


def _p(cc="966"):
    return PhoneIdentifier(default_cc=cc)


@pytest.mark.parametrize("raw", ["0501234567", "+966501234567", "966501234567",
                                 "+966 50 123 4567", "050 123 4567", "0501234567 ",
                                 "00966501234567", "+966-50-123-4567"])
def test_every_spelling_folds_to_one_canonical_key(raw):
    assert _p().normalize(raw) == "+966501234567"


def test_uniqueness_survives_normalization_one_human_not_three():
    p = _p()
    keys = {p.normalize("0501234567"), p.normalize("+966501234567"), p.normalize("966501234567")}
    assert keys == {"+966501234567"}, "three spellings became more than one identity"


def test_the_default_country_code_is_config_not_hardcoded():
    # A national number resolves with the configured code — works for any market.
    assert _p(cc="20").normalize("01001234567") == "+201001234567"      # Egypt
    assert _p(cc="971").normalize("0501234567") == "+971501234567"      # UAE


def test_validate_accepts_a_real_number_and_rejects_garbage():
    _p().validate("0501234567")
    _p().validate("+966501234567")
    for bad in ["", "   ", "abc", "+", "12", "+0123456789"]:   # empty, non-numeric, too short, leading zero
        with pytest.raises(ValidationError):
            _p().validate(bad)


def test_normalize_is_idempotent():
    p = _p()
    once = p.normalize("0501234567")
    assert p.normalize(once) == once            # keying an already-normalized value is stable


def test_display_is_the_canonical_e164():
    assert _p().display("0501234567") == "+966501234567"


def test_registered_in_the_container():
    from app.container import _LOGIN_IDENTIFIER
    assert "phone" in _LOGIN_IDENTIFIER
