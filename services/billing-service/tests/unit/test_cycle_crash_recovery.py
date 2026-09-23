"""Crash-window recovery for the renewal cycle (audit MEDIUM): a hard crash mid-charge must never let a
re-run cut a SECOND invoice / double-charge. Marker-first — invoice + idempo mark are durable before
the period advances and before the charge — so a re-run replays the invoice and completes the advance.
"""
from __future__ import annotations

import pytest

from app.adapters.datastore.memory import MemoryDataStore
from app.core.stores import PaymentStore, SubscriptionStore

from tests.unit.test_consolidated_cycle import _invoices, _svc, _two_due_subs, run


def _payments(st):
    return run(st.scan_all("payments", 100))


def _boom_once(b):
    """Patch _charge_invoice to raise on its first call (a SIGKILL mid-charge), then behave normally."""
    orig = b._charge_invoice
    state = {"n": 0}

    async def patched(inv, token, sub=None):
        state["n"] += 1
        if state["n"] == 1:
            raise RuntimeError("SIGKILL mid-charge")
        return await orig(inv, token, sub)

    b._charge_invoice = patched


def test_run_cycle_crash_mid_charge_does_not_double_invoice():
    st = MemoryDataStore(); b = _svc(st, "per_subscription")
    s1, _s2 = _two_due_subs(b, st)
    pe = run(SubscriptionStore(st).fetch(s1.id)).current_period_end
    _boom_once(b)
    with pytest.raises(RuntimeError):
        run(b.run_cycle(s1.id))
    # invoice saved + marked, but the period did NOT advance (crash before the advance)
    assert run(SubscriptionStore(st).fetch(s1.id)).current_period_end == pe
    assert len([i for i in _invoices(st) if i["subscription_id"] == s1.id]) == 1
    # re-run: replays the SAME invoice and completes the advance — no second invoice
    run(b.run_cycle(s1.id))
    assert len([i for i in _invoices(st) if i["subscription_id"] == s1.id]) == 1   # still ONE
    assert run(SubscriptionStore(st).fetch(s1.id)).current_period_end > pe          # advanced now


def test_consolidated_crash_mid_charge_does_not_double_invoice():
    st = MemoryDataStore(); b = _svc(st, "consolidated")
    s1, s2 = _two_due_subs(b, st)
    subs = SubscriptionStore(st)
    pe1 = run(subs.fetch(s1.id)).current_period_end
    _boom_once(b)
    group = [run(subs.fetch(s1.id)), run(subs.fetch(s2.id))]
    with pytest.raises(RuntimeError):
        run(b.run_consolidated_cycle(group))        # one invoice for the group, crash mid-charge
    assert len(_invoices(st)) == 1                   # the group invoice was cut + marked before the crash
    assert run(subs.fetch(s1.id)).current_period_end == pe1   # subs un-advanced (crash before advance)
    # re-run with freshly-read (un-advanced) subs: replays the SAME invoice + completes the advances
    group2 = [run(subs.fetch(s1.id)), run(subs.fetch(s2.id))]
    run(b.run_consolidated_cycle(group2))
    assert len(_invoices(st)) == 1                   # still exactly ONE invoice for the group
    assert run(subs.fetch(s1.id)).current_period_end > pe1
    assert run(subs.fetch(s2.id)).current_period_end > pe1
