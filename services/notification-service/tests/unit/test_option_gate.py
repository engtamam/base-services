"""OptionGate (notification) — native_pay defaults OFF; unknown refused; platform lock wins."""
from __future__ import annotations

import asyncio

import pytest

from app.adapters.datastore.memory import MemoryDataStore
from app.core.option_gate import OptionGate, OptionLocked, UnknownOption


def run(c):
    return asyncio.run(c)


def _gate():
    return OptionGate(MemoryDataStore(), now=lambda: "2026-09-19T00:00:00+00:00")


def test_native_pay_defaults_off():
    g = _gate()
    assert run(g.is_enabled("acme", "native_pay")) is False
    st = run(g.status(tenant="acme", option="native_pay"))
    assert st.source == "default" and st.locked is False


def test_unknown_option_refused():
    with pytest.raises(UnknownOption):
        run(_gate().is_enabled("acme", "external_paylink"))   # that option lives in payments-service


def test_tenant_opt_in_then_platform_lock_off_wins():
    g = _gate()
    run(g.set(tenant="acme", option="native_pay", enabled=True))
    assert run(g.is_enabled("acme", "native_pay")) is True
    run(g.set(tenant="", option="native_pay", enabled=False, locked=True))
    assert run(g.is_enabled("acme", "native_pay")) is False


def test_locked_scope_needs_force():
    g = _gate()
    run(g.set(tenant="acme", option="native_pay", enabled=True, locked=True))
    with pytest.raises(OptionLocked):
        run(g.set(tenant="acme", option="native_pay", enabled=False))
    assert run(g.set(tenant="acme", option="native_pay", enabled=False, force=True)).enabled is False
