"""The materialized ledger balance, against a REAL database.

`LedgerStore.record()` advances a stored balance under compare-and-swap. On the in-memory
adapter an `asyncio.Lock` serialises every writer, so a lost update is impossible there by
construction — the test would pass on code that cannot work. Only a real server lets two
writers actually race.

What must hold, no matter the interleaving:

  * the stored balance equals the sum of the rows (`reconcile().consistent`);
  * no charge is lost — the sum equals what we asked for;
  * a losing CAS writes NOTHING, so a contention error can never leave a half-applied charge.
"""
from __future__ import annotations

import asyncio
import os

import pytest

from app.core.stores import LedgerStore
from tests.contracts.util import run

_PG_DSN = os.environ.get("DATASTORE_TEST_DSN", "")

pytestmark = pytest.mark.skipif(
    not _PG_DSN, reason="set DATASTORE_TEST_DSN — a lock in the memory adapter would fake this")


@pytest.fixture
def store():
    from app.adapters.datastore.postgres import PostgresDataStore

    s = PostgresDataStore(_PG_DSN)

    async def _reset():
        pool = await s._ensure()
        await pool.execute("TRUNCATE kv_store")

    run(_reset())
    yield s
    run(s.close())


def test_concurrent_charges_do_not_lose_a_single_minor_unit(store):
    ledger = LedgerStore(store)
    n, amount = 50, 100

    async def _charge(i: int):
        return await ledger.record(tenant="t1", kind="charge", amount_minor=amount,
                                   currency="SAR", source=f"src-{i}")

    async def _all():
        return await asyncio.gather(*[_charge(i) for i in range(n)], return_exceptions=True)

    results = run(_all())
    failures = [r for r in results if isinstance(r, Exception)]
    assert not failures, f"{len(failures)} of {n} charges raised: {failures[:2]}"

    assert run(ledger.balance("t1")) == n * amount

    report = run(ledger.reconcile("t1"))
    assert report["consistent"], report
    assert report["entries"] == n
    assert report["derived"] == n * amount


def test_every_row_records_a_distinct_balance_after(store):
    """`balance_after` is written into an immutable row. Two rows claiming the same balance
    means one of them recorded a balance the ledger never actually held."""
    ledger = LedgerStore(store)
    n = 30

    async def _all():
        return await asyncio.gather(*[
            ledger.record(tenant="t1", kind="charge", amount_minor=10, currency="SAR",
                          source=f"s{i}") for i in range(n)])

    entries = run(_all())
    seen = sorted(e["balance_after"] for e in entries)
    assert seen == [10 * (i + 1) for i in range(n)], "a balance_after was duplicated or skipped"


def test_mixed_charges_and_refunds_reconcile(store):
    """A refund is a negative delta. `incr_if_below` clamps at zero and would corrupt it —
    which is why the ledger uses `cas` instead."""
    ledger = LedgerStore(store)

    async def _all():
        charges = [ledger.record(tenant="t2", kind="charge", amount_minor=500, currency="SAR",
                                 source=f"c{i}") for i in range(20)]
        refunds = [ledger.record(tenant="t2", kind="refund", amount_minor=-200, currency="SAR",
                                 source=f"r{i}") for i in range(20)]
        return await asyncio.gather(*(charges + refunds))

    run(_all())
    expected = 20 * 500 - 20 * 200
    assert run(ledger.balance("t2")) == expected
    report = run(ledger.reconcile("t2"))
    assert report["consistent"] and report["derived"] == expected


def test_one_tenants_writes_never_move_another_tenants_balance(store):
    ledger = LedgerStore(store)

    async def _all():
        return await asyncio.gather(*(
            [ledger.record(tenant="a", kind="charge", amount_minor=7, currency="SAR",
                           source=f"a{i}") for i in range(25)] +
            [ledger.record(tenant="b", kind="charge", amount_minor=11, currency="SAR",
                           source=f"b{i}") for i in range(25)]))

    run(_all())
    assert run(ledger.balance("a")) == 25 * 7
    assert run(ledger.balance("b")) == 25 * 11
    assert run(ledger.reconcile("a"))["consistent"]
    assert run(ledger.reconcile("b"))["consistent"]
