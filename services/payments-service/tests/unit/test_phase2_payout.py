"""Phase 2 — payout tracking + reconcile, tested adversarially.

A payout event must fire EXACTLY ONCE per settlement (terminal transition), survive replays and races,
and never be settled off a forged/unverified webhook body (verify-by-fetch is the anchor). The sweep is
bounded/idempotent/per-row isolated.
"""
from __future__ import annotations

import asyncio
import json

import pytest

from app.adapters.datastore.memory import MemoryDataStore
from app.adapters.event_bus.memory import MemoryEventBus
from app.core.payment_requests import PaymentRequestStore
from app.core.payout import PayoutStore
from app.core.payments import PaymentService
from app.core.reconcile_sweep import run_payout_sweep_once
from app.ports.payment_gateway import (CAP_CHECKOUT, CAP_PAYOUT, CAP_SPLIT, MODEL_MARKETPLACE,
                                       PAYOUT_FAILED, PAYOUT_PAID, PAYOUT_PENDING, PayoutStatus)


def run(c):
    return asyncio.run(c)


# ═══════════════════════════════════ PayoutStore ═════════════════════════════════
def _ps():
    return PayoutStore(MemoryDataStore())


def test_new_pending_is_recorded_not_transitioned():
    s = _ps()
    t, row = run(s.apply(payout_ref="po1", status=PAYOUT_PENDING, amount_minor=5000, currency="SAR"))
    assert t is False and row["status"] == PAYOUT_PENDING


def test_first_observation_terminal_counts_as_transition():
    s = _ps()
    t, row = run(s.apply(payout_ref="po1", status=PAYOUT_PAID, amount_minor=5000, currency="SAR"))
    assert t is True and row["status"] == PAYOUT_PAID


def test_pending_then_paid_transitions_once():
    s = _ps()
    run(s.apply(payout_ref="po1", status=PAYOUT_PENDING))
    t, row = run(s.apply(payout_ref="po1", status=PAYOUT_PAID, amount_minor=5000, currency="SAR"))
    assert t is True and row["status"] == PAYOUT_PAID


def test_replayed_terminal_is_idempotent_noop():
    s = _ps()
    run(s.apply(payout_ref="po1", status=PAYOUT_PAID))
    t, _ = run(s.apply(payout_ref="po1", status=PAYOUT_PAID))
    assert t is False


def test_terminal_is_never_overwritten_by_a_different_terminal():
    s = _ps()
    run(s.apply(payout_ref="po1", status=PAYOUT_PAID))
    t, row = run(s.apply(payout_ref="po1", status=PAYOUT_FAILED))
    assert t is False and row["status"] == PAYOUT_PAID        # first terminal wins


def test_pending_page_returns_only_pending():
    s = _ps()
    run(s.apply(payout_ref="p_pending", status=PAYOUT_PENDING))
    run(s.apply(payout_ref="p_paid", status=PAYOUT_PAID))
    rows, _ = run(s.pending_page(limit=50))
    refs = {r["payout_ref"] for r in rows}
    assert "p_pending" in refs and "p_paid" not in refs


def test_empty_payout_ref_is_refused():
    with pytest.raises(ValueError):
        run(_ps().apply(payout_ref="", status=PAYOUT_PAID))


# ═══════════════════════════ PaymentService payout paths ═════════════════════════
class _Bus:
    def __init__(self):
        self.events = []

    async def publish(self, topic, ev):
        self.events.append(ev)

    def of(self, kind):
        return [e for e in self.events if e.type == kind]


class _FakeMkt:
    driver = "fake_mkt"
    model = MODEL_MARKETPLACE
    capabilities = frozenset({CAP_CHECKOUT, CAP_SPLIT, CAP_PAYOUT})
    onboarding_modes = frozenset()

    def __init__(self, status=PAYOUT_PAID, verified=True, amount=5000):
        self._status, self._verified, self._amount = status, verified, amount
        self.fetches = 0

    def fetch_payout_status(self, *, payout_ref):
        self.fetches += 1
        return PayoutStatus(status=self._status, provider=self.driver, payout_ref=payout_ref,
                            amount_minor=self._amount, currency="SAR", verified=self._verified)


class _Resolver:
    def __init__(self, gw):
        self._gw = gw

    async def gateway(self):
        return self._gw

    async def configured(self):
        return self._gw is not None


def _svc(gw, store=None):
    store = store or MemoryDataStore()
    bus = _Bus()
    svc = PaymentService(
        requests=PaymentRequestStore(store), gateways=None, event_bus=bus,
        redirect_base_url="https://c.example", allowed_hosts=frozenset(), link_ttl_seconds=3600,
        marketplace=_Resolver(gw) if gw is not None else None, submerchants=None,
        payouts=PayoutStore(store))
    return svc, bus, store


def test_apply_payout_emits_settled_once():
    svc, bus, _ = _svc(_FakeMkt())
    run(svc.apply_payout(PayoutStatus(status=PAYOUT_PAID, payout_ref="po1", amount_minor=5000,
                                      currency="SAR", provider="fake_mkt")))
    run(svc.apply_payout(PayoutStatus(status=PAYOUT_PAID, payout_ref="po1", amount_minor=5000,
                                      currency="SAR", provider="fake_mkt")))   # replay
    assert len(bus.of("payout.settled")) == 1                 # exactly once


def test_apply_payout_emits_failed():
    svc, bus, _ = _svc(_FakeMkt())
    run(svc.apply_payout(PayoutStatus(status=PAYOUT_FAILED, payout_ref="po2", provider="fake_mkt")))
    assert len(bus.of("payout.failed")) == 1


def test_payout_event_carries_no_pii():
    svc, bus, _ = _svc(_FakeMkt())
    run(svc.apply_payout(PayoutStatus(status=PAYOUT_PAID, payout_ref="po1", amount_minor=5000,
                                      currency="SAR", provider="fake_mkt")))
    p = bus.of("payout.settled")[0].payload
    assert set(p) == {"tenant", "payout_ref", "destination_id", "status", "amount_minor",
                      "currency", "provider"}


def test_handle_payout_webhook_verifies_by_fetch():
    gw = _FakeMkt(status=PAYOUT_PAID, verified=True)
    svc, bus, _ = _svc(gw)
    body = json.dumps({"id": "po1"}).encode()
    row = run(svc.handle_payout_webhook(headers={}, body=body))
    assert row is not None and row["status"] == PAYOUT_PAID
    assert gw.fetches == 1 and len(bus.of("payout.settled")) == 1


def test_handle_payout_webhook_unverified_fetch_settles_nothing():
    gw = _FakeMkt(status=PAYOUT_PAID, verified=False)
    svc, bus, _ = _svc(gw)
    row = run(svc.handle_payout_webhook(headers={}, body=json.dumps({"id": "po1"}).encode()))
    assert row is None and bus.of("payout.settled") == []


def test_handle_payout_webhook_no_id_is_ignored():
    gw = _FakeMkt()
    svc, _, _ = _svc(gw)
    assert run(svc.handle_payout_webhook(headers={}, body=json.dumps({"x": 1}).encode())) is None
    assert gw.fetches == 0                                    # no id → no authenticated fetch


def test_reconcile_payout_applies_terminal():
    gw = _FakeMkt(status=PAYOUT_PAID)
    svc, bus, _ = _svc(gw)
    row = run(svc.reconcile_payout("po1"))
    assert row["status"] == PAYOUT_PAID and len(bus.of("payout.settled")) == 1


# ═══════════════════════════════ payout sweep ════════════════════════════════════
def test_payout_sweep_reconciles_pending_and_emits():
    gw = _FakeMkt(status=PAYOUT_PAID)
    svc, bus, store = _svc(gw)
    payouts = PayoutStore(store)
    run(payouts.apply(payout_ref="po1", status=PAYOUT_PENDING))
    run(payouts.apply(payout_ref="po2", status=PAYOUT_PENDING))
    stats = run(run_payout_sweep_once(payouts=payouts, service=svc, page_size=50))
    assert stats.scanned == 2 and stats.reconciled == 2
    assert len(bus.of("payout.settled")) == 2
    # a second sweep finds nothing pending (both now terminal) — idempotent
    stats2 = run(run_payout_sweep_once(payouts=payouts, service=svc, page_size=50))
    assert stats2.scanned == 0


def test_payout_sweep_isolates_a_bad_row():
    class _BoomMkt(_FakeMkt):
        def fetch_payout_status(self, *, payout_ref):
            raise RuntimeError("provider down")
    svc, bus, store = _svc(_BoomMkt())
    payouts = PayoutStore(store)
    run(payouts.apply(payout_ref="po1", status=PAYOUT_PENDING))
    stats = run(run_payout_sweep_once(payouts=payouts, service=svc, page_size=50))
    assert stats.errors == 1                                  # logged + skipped, not aborted
