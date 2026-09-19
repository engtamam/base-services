"""OptionGate — per-tenant per-option enable/lock semantics.

The load-bearing rules: an unknown option is refused (a typo must never read as enabled), the
built-in default applies with no row, a tenant override decides that shop, a PLATFORM lock is the
fleet-wide hard override, and a locked scope refuses a change/clear without force.
"""
from __future__ import annotations

import asyncio

import pytest

from app.adapters.datastore.memory import MemoryDataStore
from app.core.option_gate import OptionGate, OptionLocked, UnknownOption


def run(c):
    return asyncio.run(c)


def _gate():
    return OptionGate(MemoryDataStore(), known={"external_paylink": True, "beta_thing": False},
                      now=lambda: "2026-09-19T00:00:00+00:00")


def test_default_applies_with_no_row():
    g = _gate()
    assert run(g.is_enabled("acme", "external_paylink")) is True
    assert run(g.is_enabled("acme", "beta_thing")) is False
    st = run(g.status(tenant="acme", option="external_paylink"))
    assert st.source == "default" and st.locked is False


def test_unknown_option_is_refused_never_enabled():
    g = _gate()
    with pytest.raises(UnknownOption):
        run(g.is_enabled("acme", "teleport"))
    with pytest.raises(UnknownOption):
        run(g.set(tenant="acme", option="teleport", enabled=True))


def test_tenant_override_decides_that_shop_only():
    g = _gate()
    run(g.set(tenant="acme", option="external_paylink", enabled=False))
    assert run(g.is_enabled("acme", "external_paylink")) is False
    assert run(g.is_enabled("other", "external_paylink")) is True   # default, untouched


def test_platform_lock_overrides_a_tenant_row():
    g = _gate()
    run(g.set(tenant="acme", option="external_paylink", enabled=True))     # tenant wants it on
    run(g.set(tenant="", option="external_paylink", enabled=False, locked=True))  # platform off+lock
    st = run(g.status(tenant="acme", option="external_paylink"))
    assert st.enabled is False and st.locked is True and st.source == "override"


def test_a_platform_default_row_applies_when_unlocked_and_no_tenant_row():
    g = _gate()
    run(g.set(tenant="", option="external_paylink", enabled=False))   # platform default off, no lock
    assert run(g.is_enabled("acme", "external_paylink")) is False
    run(g.set(tenant="acme", option="external_paylink", enabled=True))  # a tenant may still opt in
    assert run(g.is_enabled("acme", "external_paylink")) is True


def test_a_locked_scope_refuses_a_change_and_clear_without_force():
    g = _gate()
    run(g.set(tenant="acme", option="external_paylink", enabled=False, locked=True))
    with pytest.raises(OptionLocked):
        run(g.set(tenant="acme", option="external_paylink", enabled=True))
    with pytest.raises(OptionLocked):
        run(g.clear(tenant="acme", option="external_paylink"))
    # force gets through
    st = run(g.set(tenant="acme", option="external_paylink", enabled=True, force=True))
    assert st.enabled is True
    assert run(g.clear(tenant="acme", option="external_paylink", force=True)) is True


def test_clear_returns_false_when_no_row():
    g = _gate()
    assert run(g.clear(tenant="acme", option="external_paylink")) is False
