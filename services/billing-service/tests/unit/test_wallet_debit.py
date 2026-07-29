"""The wallet `debit` primitive — 51.1.

A refund/chargeback reverses the credits a payment granted. That needs a debit the wallet port did
not have. These tests pin its contract at the adapter boundary: the HTTP adapter maps status codes
to the same retryable/permanent split as `credit`, treats insufficient-balance (409) as permanent,
sends `allow_negative`, and the null wallet debits nothing.
"""
from __future__ import annotations

import asyncio

import pytest

from app.adapters.wallet.null import NullWallet
import app.adapters.wallet.http as wh
from app.adapters.wallet.http import HttpWallet
from app.ports.wallet import Wallet, WalletRefused, WalletUnavailable


def run(c):
    return asyncio.run(c)


def _wallet(monkeypatch, status, capture=None):
    def fake_post(url, payload, headers, timeout=0):
        if capture is not None:
            capture["url"] = url
            capture["payload"] = payload
        return status, {}
    monkeypatch.setattr(wh, "post_json", fake_post)
    return HttpWallet("http://wallet-service:8000", lambda: _tok(), allowed_hosts=frozenset(),
                      timeout_seconds=5)


async def _tok():
    return "t"


def test_the_port_declares_debit_and_both_drivers_satisfy_it():
    assert isinstance(NullWallet(), Wallet)


def test_null_wallet_debits_nothing():
    assert run(NullWallet().debit(tenant="a", fund="credits", amount_minor=100, source="s")) is False


def test_http_debit_posts_to_the_debit_route_with_allow_negative(monkeypatch):
    cap: dict = {}
    w = _wallet(monkeypatch, 200, cap)
    ok = run(w.debit(tenant="acme", fund="credits", amount_minor=500,
                     source="billing:invoice:inv_1:clawback", reason="refund", allow_negative=True))
    assert ok is True
    assert cap["url"].endswith("/internal/wallet/debit")
    assert cap["payload"]["allow_negative"] is True
    assert cap["payload"]["source"].endswith(":clawback")
    assert cap["payload"]["amount"] == 500


def test_http_debit_insufficient_balance_is_permanent(monkeypatch):
    w = _wallet(monkeypatch, 409)
    with pytest.raises(WalletRefused):
        run(w.debit(tenant="a", fund="credits", amount_minor=100, source="s"))


@pytest.mark.parametrize("status", [400, 404, 422])
def test_http_debit_bad_request_is_permanent(monkeypatch, status):
    w = _wallet(monkeypatch, status)
    with pytest.raises(WalletRefused):
        run(w.debit(tenant="a", fund="credits", amount_minor=100, source="s"))


@pytest.mark.parametrize("status", [500, 502, 503])
def test_http_debit_server_error_is_retryable(monkeypatch, status):
    w = _wallet(monkeypatch, status)
    with pytest.raises(WalletUnavailable):
        run(w.debit(tenant="a", fund="credits", amount_minor=100, source="s"))
