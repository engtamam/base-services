"""The clawback outbox — 51.2. The durable reverse of a grant.

A refund/chargeback must reverse the credits a payment granted, and it must do so the way grants
are delivered: a durable row, retried, reconcilable from the void fact — never an event the bus can
drop. These tests pin the parts that make it correct: a DISTINCT idempotency key, debit-on-deliver,
grant.clawback_failed on permanent refusal, and reconcile healing from a voided invoice.
"""
from __future__ import annotations

import asyncio

from app.adapters.datastore.memory import MemoryDataStore
from app.core.grants import (DELIVERED, FAILED, PENDING, Grant, GrantOutbox,
                             clawback_source_for, source_for)
from app.ports.wallet import WalletRefused, WalletUnavailable


def run(c):
    return asyncio.run(c)


CFG = {"grant_fund": "credits", "grant_retry_base_seconds": 30, "grant_retry_max_seconds": 3600,
       "clawback_allow_negative": True}


class _Clock:
    def __init__(self, t=1000.0):
        self.t = t

    def __call__(self):
        return self.t


class _Wallet:
    driver = "fake"

    def __init__(self, *, balance=0, fail=None, refuse_debit=False):
        self.credits: list = []
        self.debits: list = []
        self.balance = balance
        self._fail = fail
        self._refuse = refuse_debit

    async def credit(self, *, tenant, fund, amount_minor, source, reason="", expires_at=None):
        self.credits.append(source)
        self.balance += amount_minor
        return True

    async def debit(self, *, tenant, fund, amount_minor, source, reason="", allow_negative=False):
        if self._fail:
            raise self._fail
        if amount_minor > self.balance and not allow_negative and self._refuse:
            raise WalletRefused("insufficient")
        self.debits.append({"source": source, "amount": amount_minor, "allow_negative": allow_negative})
        self.balance -= amount_minor
        return True

    async def health(self):
        return True


class _Emitter:
    def __init__(self):
        self.events: list = []

    async def emit(self, event, payload):
        self.events.append((event, payload))


def _outbox(store, wallet, emitter=None):
    return GrantOutbox(store, wallet, clock=_Clock(), iso=lambda: "2026-07-30T00:00:00Z",
                       cfg=CFG, emitter=emitter)


# ── the distinct key is the whole correctness story ───────────────────────────
def test_clawback_key_is_distinct_from_the_grant_key():
    assert clawback_source_for("inv_1") != source_for("inv_1")
    assert clawback_source_for("inv_1").endswith(":clawback")


def test_recording_a_clawback_twice_makes_one_row():
    st = MemoryDataStore()
    ob = _outbox(st, _Wallet())
    a = run(ob.record_clawback(tenant="acme", invoice_id="inv_1", fund="credits", amount_minor=5000))
    b = run(ob.record_clawback(tenant="acme", invoice_id="inv_1", fund="credits", amount_minor=5000))
    assert a.id == b.id == clawback_source_for("inv_1")
    rows, _ = run(st.find("credit_grants", {"kind": "clawback"}, limit=10))
    assert len(rows) == 1


# ── deliver reverses via debit ────────────────────────────────────────────────
def test_delivering_a_clawback_debits_the_wallet():
    st = MemoryDataStore()
    w = _Wallet(balance=5000)
    ob = _outbox(st, w)
    row = run(ob.record_clawback(tenant="acme", invoice_id="inv_1", fund="credits",
                                 amount_minor=5000, allow_negative=True))
    assert run(ob.deliver(row)) is True
    assert w.debits and w.debits[0]["source"].endswith(":clawback")
    assert w.balance == 0


def test_a_spent_balance_with_allow_negative_goes_below_zero():
    st = MemoryDataStore()
    w = _Wallet(balance=0, refuse_debit=True)          # already spent
    ob = _outbox(st, w)
    row = run(ob.record_clawback(tenant="acme", invoice_id="inv_1", fund="credits",
                                 amount_minor=5000, allow_negative=True))
    assert run(ob.deliver(row)) is True
    assert w.balance == -5000


def test_a_spent_balance_without_allow_negative_fails_and_emits():
    st = MemoryDataStore()
    w = _Wallet(balance=0, refuse_debit=True)
    em = _Emitter()
    ob = _outbox(st, w, emitter=em)
    row = run(ob.record_clawback(tenant="acme", invoice_id="inv_1", fund="credits",
                                 amount_minor=5000, allow_negative=False))
    assert run(ob.deliver(row)) is False
    reloaded = Grant.from_dict(run(st.get("credit_grants", row.id)))
    assert reloaded.status == FAILED
    assert em.events and em.events[0][0] == "grant.clawback_failed"


def test_a_transport_failure_keeps_the_clawback_pending():
    st = MemoryDataStore()
    w = _Wallet(fail=WalletUnavailable("down"))
    ob = _outbox(st, w)
    row = run(ob.record_clawback(tenant="acme", invoice_id="inv_1", fund="credits", amount_minor=5000))
    assert run(ob.deliver(row)) is False
    reloaded = Grant.from_dict(run(st.get("credit_grants", row.id)))
    assert reloaded.status == PENDING and reloaded.attempts == 1


# ── reconcile heals a dropped clawback from the void fact ──────────────────────
class _Invoices:
    def __init__(self, voided):
        self._voided = voided

    async def find_voided(self, *, limit=200):
        return self._voided, ""


def test_reconcile_creates_a_clawback_for_a_voided_invoice_whose_grant_was_delivered():
    st = MemoryDataStore()
    ob = _outbox(st, _Wallet(balance=5000))
    # a delivered grant exists
    g = Grant(id=source_for("inv_1"), tenant="acme", source=source_for("inv_1"), fund="credits",
              amount_minor=5000, status=DELIVERED)
    run(st.put("credit_grants", g.id, g.to_dict()))
    inv = {"id": "inv_1", "tenant": "acme", "status": "void", "meta": {"grant_minor": 5000}}
    out = run(ob.reconcile_clawbacks(invoices=_Invoices([inv])))
    assert out["created"] == 1
    assert run(st.get("credit_grants", clawback_source_for("inv_1"))) is not None


def test_reconcile_does_not_claw_back_an_undelivered_grant():
    """Nothing reached the wallet, so there is nothing to take back — cancel, do not reverse."""
    st = MemoryDataStore()
    ob = _outbox(st, _Wallet())
    g = Grant(id=source_for("inv_1"), tenant="acme", source=source_for("inv_1"), fund="credits",
              amount_minor=5000, status=PENDING)
    run(st.put("credit_grants", g.id, g.to_dict()))
    inv = {"id": "inv_1", "tenant": "acme", "status": "void", "meta": {"grant_minor": 5000}}
    out = run(ob.reconcile_clawbacks(invoices=_Invoices([inv])))
    assert out["created"] == 0
    assert run(st.get("credit_grants", clawback_source_for("inv_1"))) is None
