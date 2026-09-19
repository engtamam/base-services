"""wa_inbound is a bounded reconciliation buffer, not a forever archive: rows carry a retention
window, a purge reaps the expired, and the compliance erasure flow deletes a subject's inbound
messages. Without these, a customer's inbound message content sat indefinitely and survived a DSAR."""
from __future__ import annotations

import asyncio

from app.adapters.datastore.memory import MemoryDataStore
from app.core.stores import InboundStore


def run(c):
    return asyncio.run(c)


class _Clock:
    def __init__(self, t=1000.0):
        self.t = t

    def __call__(self):
        return self.t


def _rec(store, wamid, sender="96650", ts=1.0, tenant="acme"):
    return run(store.record(wamid=wamid, sender=sender, timestamp=ts, msg_type="text",
                            tenant=tenant, phone_number_id="PN1", waba_id="WABA1",
                            message={"text": {"body": "hi"}}))


def test_record_stamps_an_expiry_from_the_retention_window():
    clock = _Clock(1000.0)
    s = InboundStore(MemoryDataStore(), clock=clock, retention_seconds=30 * 86400)
    _rec(s, "wamid.1")
    row = run(s._store.get("wa_inbound", "wamid.1"))
    assert row["expires_at"] == 1000.0 + 30 * 86400


def test_retention_zero_means_never_expires():
    s = InboundStore(MemoryDataStore(), clock=_Clock(), retention_seconds=0)
    _rec(s, "wamid.1")
    assert run(s._store.get("wa_inbound", "wamid.1"))["expires_at"] == 0.0


def test_purge_removes_expired_and_keeps_in_window():
    ds = MemoryDataStore()
    clock = _Clock(1000.0)
    s = InboundStore(ds, clock=clock, retention_seconds=100)      # expires_at = 1100
    _rec(s, "old")
    clock.t = 2000.0                                              # this one expires at 2100
    _rec(s, "fresh")
    clock.t = 1500.0                                             # now: past "old" (1100), before "fresh" (2100)
    removed = run(s.purge_expired())
    assert removed == 1
    assert run(ds.get("wa_inbound", "old")) is None
    assert run(ds.get("wa_inbound", "fresh")) is not None


def test_never_expiring_rows_are_not_purged():
    ds = MemoryDataStore()
    s = InboundStore(ds, clock=_Clock(5000.0), retention_seconds=0)   # no expiry
    _rec(s, "keep")
    assert run(s.purge_expired()) == 0
    assert run(ds.get("wa_inbound", "keep")) is not None


def test_erase_subject_deletes_that_customers_inbound_only():
    ds = MemoryDataStore()
    s = InboundStore(ds, clock=_Clock(), retention_seconds=0)
    _rec(s, "a1", sender="96650", tenant="acme")
    _rec(s, "a2", sender="96650", tenant="acme")
    _rec(s, "b1", sender="96651", tenant="acme")       # a different customer
    _rec(s, "c1", sender="96650", tenant="other")      # same number, different tenant
    removed = run(s.erase_subject(tenant="acme", subject="96650"))
    assert removed == 2
    assert run(ds.get("wa_inbound", "a1")) is None and run(ds.get("wa_inbound", "a2")) is None
    assert run(ds.get("wa_inbound", "b1")) is not None       # other customer untouched
    assert run(ds.get("wa_inbound", "c1")) is not None       # other tenant untouched
