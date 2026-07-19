"""Pause, reactivate, and the cycle that ignored both.

`pause` and `reactivate` existed with no route, so nothing exercised them — and underneath,
`run_cycle` never looked at `status` at all. Wiring the routes without this file would have
shipped a pause button that suppressed nothing: the customer sees "paused", the invoice arrives
anyway. Cancel had the same hole already.

The heaviest tests here are the two about money leaving a stopped subscription.
"""
from __future__ import annotations

import asyncio

import pytest

from app.core.billing_service import _BILLABLE


def run(c):
    return asyncio.run(c)


class _Subs:
    """SubscriptionStore stand-in — fetch/save is the whole surface these paths touch."""

    def __init__(self, sub):
        self.sub = sub
        self.saves = 0

    async def fetch(self, sub_id):
        return self.sub if self.sub and self.sub.id == sub_id else None

    async def save(self, sub):
        self.saves += 1
        self.sub = sub
        return sub


class _Sub:
    def __init__(self, status="active", cancel_at_period_end=False):
        self.id = "sub_1"
        self.tenant = "acme"
        self.status = status
        self.cancel_at_period_end = cancel_at_period_end


def _svc(sub):
    """A BillingService with only the collaborators these two paths use."""
    from app.core.billing_service import BillingService
    svc = BillingService.__new__(BillingService)      # no full container: this is a state machine
    svc._subs = _Subs(sub)
    return svc


# ── pause ──────────────────────────────────────────────────────────────────────
def test_pause_moves_an_active_subscription_to_paused():
    svc = _svc(_Sub("active"))
    assert run(svc.pause("sub_1")).status == "paused"


def test_pausing_a_canceled_subscription_is_refused():
    """Canceled is terminal. Pausing it would move it back out of a terminal state, and the
    next reactivate would then resume charging someone who had left."""
    svc = _svc(_Sub("canceled"))
    with pytest.raises(ValueError):
        run(svc.pause("sub_1"))


def test_pausing_twice_is_idempotent_not_an_error():
    """A retried click, or a retried webhook, must not 409."""
    svc = _svc(_Sub("paused"))
    assert run(svc.pause("sub_1")).status == "paused"


def test_an_unknown_subscription_is_none_never_an_exception():
    """None and ValueError mean different things to the route: 404 vs 409. Collapsing them would
    tell a caller their real subscription does not exist, and invite a duplicate."""
    assert run(_svc(None).pause("sub_1")) is None


# ── reactivate ─────────────────────────────────────────────────────────────────
def test_reactivate_returns_a_paused_subscription_to_active():
    svc = _svc(_Sub("paused"))
    assert run(svc.reactivate("sub_1")).status == "active"


def test_reactivating_a_CANCELED_subscription_is_refused():
    """THE one that matters. Without the allowed_from guard this resumed billing a customer who
    had cancelled — money taken from someone who left."""
    svc = _svc(_Sub("canceled"))
    with pytest.raises(ValueError):
        run(svc.reactivate("sub_1"))


def test_reactivating_clears_a_pending_cancel_at_period_end():
    """Someone who paused with a cancellation already scheduled and then came back has withdrawn
    the intent to leave. Leaving the flag set would end the subscription at the period boundary
    and silently undo the reactivation they just performed."""
    svc = _svc(_Sub("paused", cancel_at_period_end=True))
    sub = run(svc.reactivate("sub_1"))
    assert sub.status == "active"
    assert sub.cancel_at_period_end is False


# ── the cycle honours all of it ────────────────────────────────────────────────
def test_the_billable_set_is_an_allowlist_that_excludes_stopped_states():
    """Written as a positive list so a status added later defaults to NOT being charged. A
    denylist would make the unknown case take money, and only one of these is recoverable."""
    assert "paused" not in _BILLABLE
    assert "canceled" not in _BILLABLE
    assert "active" in _BILLABLE


@pytest.mark.parametrize("status", ["paused", "canceled"])
def test_run_cycle_refuses_to_invoice_a_stopped_subscription(status):
    """The bug beneath the missing routes: run_cycle never read status, so a paused or cancelled
    subscription was invoiced exactly like an active one."""
    svc = _svc(_Sub(status))
    with pytest.raises(ValueError) as e:
        run(svc.run_cycle("sub_1"))
    assert "NOT_BILLABLE" in str(e.value)


def test_run_cycle_still_rejects_an_unknown_subscription_distinctly():
    svc = _svc(None)
    with pytest.raises(ValueError) as e:
        run(svc.run_cycle("sub_1"))
    assert "SUB_NOT_FOUND" in str(e.value)
