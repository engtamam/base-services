"""The durable path from "they paid" to "they got what they paid for".

Every test here is about money that must not be lost and must not be duplicated. The event bus is
at-most-once, so this path exists precisely because a subscription would drop credits a customer
paid for — and the retries that make it survivable are also what make double-crediting possible.
Both failures are covered, because fixing one by causing the other is not a fix.
"""
from __future__ import annotations

import asyncio

import pytest

from app.adapters.datastore.memory import MemoryDataStore
from app.core.grants import DELIVERED, FAILED, PENDING, Grant, GrantOutbox, source_for
from app.ports.wallet import WalletRefused, WalletUnavailable

CFG = {"grant_fund": "credits", "grant_retry_base_seconds": 30,
       "grant_retry_max_seconds": 3600}


def run(c):
    return asyncio.run(c)


class _Clock:
    def __init__(self) -> None:
        self.t = 1000.0

    def __call__(self) -> float:
        return self.t


class _Wallet:
    """Records what it was asked to credit, and de-duplicates on source like the real one."""

    driver = "fake"

    def __init__(self, fail: Exception | None = None) -> None:
        self.calls: list[dict] = []
        self.applied: dict[str, int] = {}
        self._fail = fail

    async def credit(self, *, tenant, fund, amount_minor, source, reason="", expires_at=None):
        self.calls.append({"tenant": tenant, "fund": fund, "amount_minor": amount_minor,
                           "source": source, "expires_at": expires_at})
        if self._fail:
            raise self._fail
        # The real wallet de-duplicates on source. Modelling that here is the point: it is what
        # makes retrying safe, and a test that ignored it could not tell a safe retry from a
        # double credit.
        self.applied.setdefault(source, 0)
        self.applied[source] = amount_minor
        return True

    async def health(self):
        return True


def _outbox(store, wallet, clock=None):
    return GrantOutbox(store, wallet, clock=clock or _Clock(),
                       iso=lambda: "2026-07-21T00:00:00Z", cfg=CFG)


def _record(ob, invoice_id="inv_1", amount=5000, **kw):
    return run(ob.record(tenant="acme", invoice_id=invoice_id, fund="credits",
                         amount_minor=amount, **kw))


# ── granted twice: money given away ───────────────────────────────────────────
def test_recording_the_same_invoice_twice_makes_one_grant():
    """Two rows would both deliver, and the wallet would de-duplicate them — but only because it
    happens to key on the same string. Relying on the far side to clean up after us is not a
    design, it is a coincidence."""
    st = MemoryDataStore()
    ob = _outbox(st, _Wallet())
    a, b = _record(ob), _record(ob)
    assert a.id == b.id
    rows, _ = run(st.find("credit_grants", {"tenant": "acme"}, limit=10))
    assert len(rows) == 1


def test_re_recording_a_DELIVERED_grant_does_not_resurrect_it():
    """The real thing the existing-row check protects, and my first test missed it.

    Because the row id IS the source, a second record() overwrites one row either way — so
    counting rows proves nothing. What matters is the STATE: _mark_paid can legitimately run again
    (a webhook reconcile arriving after a settlement), and if that reset a delivered grant back to
    pending it would be delivered a second time. The wallet would de-duplicate it, so no money
    moves — but the outbox would churn for ever on a grant that was already honoured, and the only
    thing standing between that and a double credit would be the far side's behaviour.
    """
    st = MemoryDataStore()
    ob = _outbox(st, _Wallet())
    g = _record(ob)
    run(ob.deliver(g))
    assert run(st.get("credit_grants", g.id))["status"] == DELIVERED

    _record(ob)                       # the same invoice paid-marked again
    row = run(st.get("credit_grants", g.id))
    assert row["status"] == DELIVERED, "a delivered grant was reset to pending and would re-deliver"
    assert row["delivered_at"], "the delivery record was wiped"


def test_the_source_key_is_derived_from_the_invoice_not_a_clock():
    """Anything containing a timestamp or a random id makes every attempt look like a new credit,
    which is how a customer ends up with three times what they paid for."""
    assert source_for("inv_9") == source_for("inv_9")
    assert source_for("inv_9") != source_for("inv_8")


def test_delivering_twice_credits_the_wallet_once():
    st = MemoryDataStore()
    w = _Wallet()
    ob = _outbox(st, w)
    g = _record(ob)
    assert run(ob.deliver(g)) is True
    run(ob.deliver(g))
    assert len(w.applied) == 1, "two distinct credits were applied for one invoice"


def test_a_delivered_grant_is_not_drained_again():
    st = MemoryDataStore()
    w = _Wallet()
    ob = _outbox(st, w)
    run(ob.deliver(_record(ob)))
    before = len(w.calls)
    assert run(ob.drain())["delivered"] == 0
    assert len(w.calls) == before


# ── granted never: money taken ────────────────────────────────────────────────
def test_an_unreachable_wallet_leaves_the_grant_pending_not_lost():
    """Giving up would silently keep money the customer paid for."""
    st = MemoryDataStore()
    ob = _outbox(st, _Wallet(fail=WalletUnavailable("down")))
    g = _record(ob)
    assert run(ob.deliver(g)) is False
    row = run(st.get("credit_grants", g.id))
    assert row["status"] == PENDING and row["attempts"] == 1


def test_a_pending_grant_is_retried_until_it_works():
    st = MemoryDataStore()
    clock = _Clock()
    down = _outbox(st, _Wallet(fail=WalletUnavailable("down")), clock)
    g = _record(down)
    run(down.deliver(g))

    clock.t += 100000                      # past the backoff
    w = _Wallet()
    up = _outbox(st, w, clock)
    assert run(up.drain())["delivered"] == 1
    assert run(st.get("credit_grants", g.id))["status"] == DELIVERED


def test_backoff_defers_a_retry_that_is_not_due_yet():
    """Hammering a wallet that is already struggling is how a degradation becomes an outage."""
    st = MemoryDataStore()
    clock = _Clock()
    ob = _outbox(st, _Wallet(fail=WalletUnavailable("down")), clock)
    run(ob.deliver(_record(ob)))
    assert run(ob.drain())["deferred"] == 1


def test_the_backoff_is_capped():
    st = MemoryDataStore()
    clock = _Clock()
    ob = _outbox(st, _Wallet(fail=WalletUnavailable("down")), clock)
    g = _record(ob)
    g.attempts = 40                        # would overflow into centuries uncapped
    run(ob.deliver(g))
    assert g.next_attempt_at - clock.t <= CFG["grant_retry_max_seconds"]


# ── retried for ever: a queue nobody can drain ────────────────────────────────
def test_a_permanent_refusal_stops_rather_than_retrying_for_ever():
    """A fund that does not exist will not exist on the thousandth attempt, and a queue that only
    grows hides the misconfiguration that caused it."""
    st = MemoryDataStore()
    ob = _outbox(st, _Wallet(fail=WalletRefused("UNKNOWN_FUND")))
    g = _record(ob)
    assert run(ob.deliver(g)) is False
    assert run(st.get("credit_grants", g.id))["status"] == FAILED
    assert run(ob.drain())["considered"] == 0, "a permanently failed grant was retried"


# ── self-healing ──────────────────────────────────────────────────────────────
class _Invoices:
    def __init__(self, rows):
        self._rows = rows

    async def find_paid(self, *, limit=200):
        return self._rows[:limit], ""


def test_reconcile_rebuilds_a_grant_the_crash_lost():
    """If the process died between the payment and the grant row, the outbox has no idea anything
    was owed — but the paid invoice is still there, and it is the durable fact."""
    st = MemoryDataStore()
    ob = _outbox(st, _Wallet())
    invoices = _Invoices([{"id": "inv_lost", "tenant": "acme",
                           "meta": {"grant_minor": 2500}}])
    assert run(ob.reconcile(invoices=invoices))["created"] == 1
    assert run(st.get("credit_grants", source_for("inv_lost")))["amount_minor"] == 2500


def test_reconcile_does_not_duplicate_a_grant_that_already_exists():
    st = MemoryDataStore()
    ob = _outbox(st, _Wallet())
    _record(ob, invoice_id="inv_1", amount=2500)
    invoices = _Invoices([{"id": "inv_1", "tenant": "acme", "meta": {"grant_minor": 2500}}])
    assert run(ob.reconcile(invoices=invoices))["created"] == 0


def test_reconcile_ignores_an_invoice_that_grants_nothing():
    st = MemoryDataStore()
    ob = _outbox(st, _Wallet())
    invoices = _Invoices([{"id": "inv_x", "tenant": "acme", "meta": {}}])
    assert run(ob.reconcile(invoices=invoices))["created"] == 0


# ── amounts ───────────────────────────────────────────────────────────────────
@pytest.mark.parametrize("amount", [0, -1, -5000])
def test_a_zero_or_negative_grant_is_never_recorded(amount):
    """A zero grant is a row that delivers nothing for ever; a negative one is a refund pretending
    to be a credit."""
    st = MemoryDataStore()
    assert _record(_outbox(st, _Wallet()), amount=amount) is None
    rows, _ = run(st.find("credit_grants", {"tenant": "acme"}, limit=10))
    assert rows == []


# ── rollover vs reset ─────────────────────────────────────────────────────────
def test_an_expiring_grant_keeps_its_original_expiry_across_a_retry():
    """A retry days later must still deliver the expiry the credits were SOLD with, not one
    computed from whenever the retry happened to succeed."""
    st = MemoryDataStore()
    clock = _Clock()
    down = _outbox(st, _Wallet(fail=WalletUnavailable("down")), clock)
    g = _record(down, expires_at=5000.0)
    run(down.deliver(g))

    clock.t += 100000
    w = _Wallet()
    run(_outbox(st, w, clock).drain())
    assert w.calls[-1]["expires_at"] == 5000.0


def test_an_accumulating_grant_has_no_expiry():
    st = MemoryDataStore()
    w = _Wallet()
    ob = _outbox(st, w)
    run(ob.deliver(_record(ob)))
    assert w.calls[-1]["expires_at"] is None


# ── no wallet wired ───────────────────────────────────────────────────────────
def test_with_no_wallet_the_promise_is_still_recorded():
    """Switching a wallet on later must deliver everything that accumulated, rather than starting
    from empty and quietly owing every customer who paid in the meantime."""
    st = MemoryDataStore()
    ob = _outbox(st, None)
    g = _record(ob)
    assert run(ob.deliver(g)) is False
    assert run(st.get("credit_grants", g.id))["status"] == PENDING

    w = _Wallet()
    assert run(_outbox(st, w).drain())["delivered"] == 1
