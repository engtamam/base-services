"""A fund can be marked withdrawable — the typed-lot foundation for payouts. Locked is the safe
default: a customer's promo credits never become cash payable to a bank by omission; only a fund an
operator deliberately marks (an affiliate's commission) can leave the platform."""
from __future__ import annotations

import pytest

from app.core import funds as _funds
from app.config import Settings
from app.ports.errors import ConfigError


def _settings(spec: str) -> Settings:
    return Settings(INTERNAL_SERVICE_TOKEN="t" * 40, WALLET_FUNDS=spec)


def test_withdrawability_defaults_to_locked_when_omitted():
    assert _settings("promo:1:untaxed").wallet_funds == (("promo", 1, False, False),)


def test_a_fund_can_be_declared_withdrawable():
    funds = _settings("promo:1:untaxed,commission:2:taxed:withdrawable").wallet_funds
    assert funds == (("promo", 1, False, False), ("commission", 2, True, True))


def test_locked_is_accepted_explicitly():
    assert _settings("cash:1:taxed:locked").wallet_funds == (("cash", 1, True, False),)


def test_a_bad_withdrawability_is_refused_at_boot():
    with pytest.raises(ConfigError):
        _settings("cash:1:taxed:maybe").wallet_funds


def test_fund_parse_carries_withdrawable_and_lists_the_payable_ones():
    funds = _funds.parse(_settings("promo:1:untaxed,commission:2:taxed:withdrawable").wallet_funds)
    assert funds[0].withdrawable is False and funds[1].withdrawable is True
    assert _funds.withdrawable_names(funds) == ("commission",)


def test_parse_still_accepts_a_legacy_3_tuple_as_locked():
    # Older specs / tests that pass (name, priority, taxable) keep working and stay locked.
    funds = _funds.parse((("cash", 1, True),))
    assert funds[0].withdrawable is False
