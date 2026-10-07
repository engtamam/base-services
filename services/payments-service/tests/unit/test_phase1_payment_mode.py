"""Phase 1 — the single-select payment-mode primitive + its enforcement wiring in create_link.

Exactly-one per tenant, platform force/lock precedence, fail-closed on unknown/tampered modes, and
cross-tenant isolation. Written adversarially: a tenant must never resolve to two modes, never hijack
the platform scope, and a tenant switched to a model this build cannot route must be refused cleanly
(claim released, nothing persisted) rather than billed on the wrong rail.
"""
from __future__ import annotations

import asyncio

import pytest

from app.adapters.datastore.memory import MemoryDataStore
from app.adapters.event_bus.memory import MemoryEventBus
from app.core.merchant_credentials import DRIVER, WEBHOOK_SECRET, MerchantGateways, merchant_secret_store
from app.core.payment_mode import (DEFAULT_MODE, KNOWN_MODES, ModeLocked, PaymentModeGate, UnknownMode,
                                   _COLL)
from app.core.payment_requests import PaymentRequestStore
from app.core.payments import PaymentNotConfigured, PaymentService
from app.ports.payment_gateway import MODEL_CONDUIT, MODEL_MARKETPLACE

KEK = "k" * 40


def run(c):
    return asyncio.run(c)


def _gate():
    return PaymentModeGate(MemoryDataStore())


# ── defaults + exactly-one ──────────────────────────────────────────────────────
def test_default_mode_is_conduit_with_no_rows():
    g = _gate()
    assert run(g.selected("acme")) == MODEL_CONDUIT == DEFAULT_MODE
    assert run(g.status(tenant="acme")).source == "default"


def test_known_modes_are_conduit_and_marketplace():
    assert KNOWN_MODES == {MODEL_CONDUIT, MODEL_MARKETPLACE}


def test_set_then_read_tenant_selection():
    g = _gate()
    run(g.set(tenant="acme", mode=MODEL_MARKETPLACE))
    st = run(g.status(tenant="acme"))
    assert st.mode == MODEL_MARKETPLACE and st.source == "tenant"


def test_selection_is_exactly_one_set_replaces_not_accumulates():
    g = _gate()
    run(g.set(tenant="acme", mode=MODEL_CONDUIT))
    run(g.set(tenant="acme", mode=MODEL_MARKETPLACE))
    # Exactly one active mode — the second set REPLACED the first; there is no "both".
    assert run(g.selected("acme")) == MODEL_MARKETPLACE
    g2store = g._s
    # exactly one row for this tenant
    rows, _ = run(g2store.find(_COLL, {"tenant": "acme"}, limit=10))
    assert len(rows) == 1


# ── unknown / tampered modes are refused / ignored (fail-closed) ────────────────
def test_set_unknown_mode_is_refused():
    g = _gate()
    with pytest.raises(UnknownMode):
        run(g.set(tenant="acme", mode="evil"))


def test_tampered_tenant_row_with_unknown_mode_is_ignored_on_read():
    g = _gate()
    # Simulate a corrupted/injected row the write path would never allow.
    run(g._s.put(_COLL, g._key("acme"), {"id": g._key("acme"), "tenant": "acme", "mode": "evil"}))
    assert run(g.selected("acme")) == MODEL_CONDUIT      # falls through to the safe default


def test_tampered_platform_lock_with_unknown_mode_is_ignored():
    g = _gate()
    run(g._s.put(_COLL, g._key(""), {"id": g._key(""), "tenant": "", "mode": "evil", "locked": True}))
    assert run(g.selected("acme")) == MODEL_CONDUIT


# ── platform force / lock precedence ────────────────────────────────────────────
def test_platform_lock_forces_one_mode_fleetwide():
    g = _gate()
    run(g.set(tenant="acme", mode=MODEL_CONDUIT))         # a tenant tries to choose conduit
    run(g.set(tenant="", mode=MODEL_MARKETPLACE, locked=True))   # platform forces marketplace
    assert run(g.selected("acme")) == MODEL_MARKETPLACE   # platform lock WINS over the tenant choice
    assert run(g.status(tenant="acme")).source == "platform"


def test_tenant_choice_wins_over_unlocked_platform_default():
    g = _gate()
    run(g.set(tenant="", mode=MODEL_MARKETPLACE))         # platform DEFAULT (unlocked)
    run(g.set(tenant="acme", mode=MODEL_CONDUIT))
    assert run(g.selected("acme")) == MODEL_CONDUIT
    assert run(g.selected("other")) == MODEL_MARKETPLACE  # a tenant with no row inherits the default


def test_platform_default_applies_without_a_tenant_row():
    g = _gate()
    run(g.set(tenant="", mode=MODEL_MARKETPLACE))
    assert run(g.selected("nobody")) == MODEL_MARKETPLACE


# ── lock guard on writes ────────────────────────────────────────────────────────
def test_locked_scope_refuses_change_without_force():
    g = _gate()
    run(g.set(tenant="acme", mode=MODEL_CONDUIT, locked=True))
    with pytest.raises(ModeLocked):
        run(g.set(tenant="acme", mode=MODEL_MARKETPLACE))
    run(g.set(tenant="acme", mode=MODEL_MARKETPLACE, force=True))   # force overrides
    assert run(g.selected("acme")) == MODEL_MARKETPLACE


def test_locked_scope_refuses_clear_without_force():
    g = _gate()
    run(g.set(tenant="acme", mode=MODEL_CONDUIT, locked=True))
    with pytest.raises(ModeLocked):
        run(g.clear(tenant="acme"))
    assert run(g.clear(tenant="acme", force=True)) is True


def test_clear_nonexistent_returns_false():
    assert run(_gate().clear(tenant="ghost")) is False


def test_clear_restores_default():
    g = _gate()
    run(g.set(tenant="acme", mode=MODEL_MARKETPLACE))
    run(g.clear(tenant="acme"))
    assert run(g.selected("acme")) == MODEL_CONDUIT


# ── cross-tenant isolation (attacker view) ──────────────────────────────────────
def test_one_tenant_selection_does_not_leak_to_another():
    g = _gate()
    run(g.set(tenant="a", mode=MODEL_MARKETPLACE))
    assert run(g.selected("a")) == MODEL_MARKETPLACE
    assert run(g.selected("b")) == MODEL_CONDUIT          # unaffected


def test_tenant_named_star_cannot_hijack_the_platform_scope():
    # The length-prefixed key makes a tenant literally named "*" distinct from the platform ("") scope,
    # so it can neither read nor overwrite the fleet-wide selection.
    g = _gate()
    run(g.set(tenant="*", mode=MODEL_MARKETPLACE))        # a hostile tenant id
    assert run(g.selected("victim")) == MODEL_CONDUIT     # platform/default untouched
    assert run(g.status(tenant="victim")).source == "default"
    assert g._key("*") != g._key("")


# ── enforcement wiring in create_link ───────────────────────────────────────────
def _console_svc(select_mode):
    store = MemoryDataStore()
    secrets = merchant_secret_store(store, KEK)
    run(secrets.set(tenant="acme", name=DRIVER, value="console"))
    run(secrets.set(tenant="acme", name=WEBHOOK_SECRET, value="s" * 20))
    svc = PaymentService(
        requests=PaymentRequestStore(store),
        gateways=MerchantGateways(secrets, console_link_base="https://pay.example.test/checkout",
                                  allow_console=True),
        event_bus=MemoryEventBus(), redirect_base_url="https://c.example",
        allowed_hosts=frozenset({"pay.example.test"}), link_ttl_seconds=3600,
        select_mode=select_mode)
    return svc, store


def test_create_link_defaults_to_conduit_when_no_resolver():
    svc, _ = _console_svc(None)
    link = run(svc.create_link(tenant="acme", reference="o1", amount_minor=1000, currency="SAR"))
    assert link.request_id


def test_create_link_works_when_mode_resolver_returns_conduit():
    async def sel(_t):
        return MODEL_CONDUIT
    svc, _ = _console_svc(sel)
    assert run(svc.create_link(tenant="acme", reference="o2", amount_minor=1000, currency="SAR")).request_id


def test_create_link_treats_empty_mode_as_conduit():
    async def sel(_t):
        return ""
    svc, _ = _console_svc(sel)
    assert run(svc.create_link(tenant="acme", reference="o3", amount_minor=1000, currency="SAR")).request_id


def test_create_link_refuses_a_not_yet_routable_mode():
    async def sel(_t):
        return MODEL_MARKETPLACE
    svc, store = _console_svc(sel)
    with pytest.raises(PaymentNotConfigured):
        run(svc.create_link(tenant="acme", reference="o4", amount_minor=1000, currency="SAR"))
    # Fail-closed BEFORE any claim/request: nothing persisted, and the order id is free to retry later.
    assert run(PaymentRequestStore(store).active_by_reference(tenant="acme", reference="o4")) is None


def test_refused_mode_leaves_the_order_id_reusable():
    # Switching the tenant back to conduit must let the same order id mint normally — the marketplace
    # refusal must not have stuck a claim on it.
    seen = {"mode": MODEL_MARKETPLACE}

    async def sel(_t):
        return seen["mode"]
    svc, _ = _console_svc(sel)
    with pytest.raises(PaymentNotConfigured):
        run(svc.create_link(tenant="acme", reference="ord-x", amount_minor=1000, currency="SAR"))
    seen["mode"] = MODEL_CONDUIT
    assert run(svc.create_link(tenant="acme", reference="ord-x", amount_minor=1000, currency="SAR")).request_id
