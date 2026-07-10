"""Non-functional guards (per memory `phase-doc-rules`).

Balance reads sit in front of every spend, and spend is the write path.

  * "Balance is derived, never stored" is the right INVARIANT and the wrong IMPLEMENTATION:
    summing the ledger on every read is an O(n) hot path that grows forever. So the wallet
    row is a projection, and `reconcile()` proves it equals the ledger.
  * Reserved totals live IN the wallet row. Deriving them by scanning the holds table would
    put a full scan on every spend.
  * Idempotency is a KEYED lookup. Answering "seen this source?" with a ledger scan (as the
    reference implementation does, backed by an index we do not have) turns every spend into
    a full table scan.
"""
from __future__ import annotations

import asyncio
import pathlib
import re

from app.adapters.datastore.memory import MemoryDataStore
from app.core.wallet_service import WalletService

CFG = {"funds": (("promo", 1, False), ("cash", 2, True)), "vat_bp": 1500, "currency": "SAR",
       "hold_ttl": 900, "hold_sweep_limit": 500, "grant_sweep_limit": 500,
       "idempotency_ttl": 3600, "ledger_list_limit": 100, "cas_max_retries": 8}


def run(c):
    return asyncio.run(c)


class Clock:
    t = 1000.0

    def now(self) -> float:
        return self.t


class CountingStore(MemoryDataStore):
    def __init__(self):
        super().__init__()
        self.reads = self.writes = self.scans = self.cas_calls = 0

    async def get(self, c, k):
        self.reads += 1
        return await super().get(c, k)

    async def put(self, c, k, v):
        self.writes += 1
        return await super().put(c, k, v)

    async def list(self, c, limit=100):
        self.scans += 1
        return await super().list(c, limit)

    async def cas(self, c, k, ev, v):
        self.cas_calls += 1
        return await super().cas(c, k, ev, v)


def _svc(store):
    return WalletService(store=store, clock=Clock(), config=CFG)


def test_reading_a_balance_never_scans():
    st = CountingStore()
    w = _svc(st)
    run(w.credit(tenant="t", fund="cash", amount=10_000, source="c"))
    st.reads = st.scans = 0
    for _ in range(100):
        run(w.balance("t"))
    assert st.scans == 0, f"balance read scanned {st.scans} times"
    assert st.reads == 100, "a balance is exactly one keyed read of the wallet row"


def test_balance_cost_does_not_grow_with_the_ledger():
    def reads_for(n):
        st = CountingStore()
        w = _svc(st)
        run(w.credit(tenant="t", fund="cash", amount=1_000_000, source="c"))
        for i in range(n):
            run(w.spend(tenant="t", net=10, source=f"s{i}"))
        st.reads = st.scans = 0
        run(w.balance("t"))
        return st.reads + st.scans

    assert reads_for(5) == reads_for(500) == 1, "balance must not sum the ledger"


def test_balance_cost_does_not_grow_with_the_number_of_holds():
    """Reserved totals live in the wallet row, so `available` needs no scan."""
    st = CountingStore()
    w = _svc(st)
    run(w.credit(tenant="t", fund="cash", amount=1_000_000, source="c"))
    for i in range(50):
        run(w.reserve(tenant="t", net=100, source=f"r{i}"))
    st.reads = st.scans = 0
    run(w.balance("t"))
    assert (st.reads, st.scans) == (1, 0)


def test_a_spend_is_one_cas_plus_one_append_and_never_scans():
    st = CountingStore()
    w = _svc(st)
    run(w.credit(tenant="t", fund="cash", amount=1_000_000, source="c"))
    st.reads = st.writes = st.scans = st.cas_calls = 0
    run(w.spend(tenant="t", net=100, source="s1"))
    assert st.scans == 0, "spend scanned the datastore"
    assert st.cas_calls == 1, f"spend used {st.cas_calls} compare-and-swaps"
    # writes: the ledger append + the idempotency record. No ledger scan for dedupe.
    assert st.writes == 2, f"unexpected write amplification: {st.writes}"


def test_spend_cost_does_not_grow_with_the_ledger():
    """Idempotency is a keyed lookup, not `SELECT ... WHERE source = ?` over the ledger."""
    def cost_for(n):
        st = CountingStore()
        w = _svc(st)
        run(w.credit(tenant="t", fund="cash", amount=10_000_000, source="c"))
        for i in range(n):
            run(w.spend(tenant="t", net=10, source=f"s{i}"))
        st.reads = st.writes = st.scans = st.cas_calls = 0
        run(w.spend(tenant="t", net=10, source="final"))
        return st.reads, st.writes, st.scans, st.cas_calls

    assert cost_for(5) == cost_for(500)


def test_a_replayed_spend_costs_nothing_and_writes_nothing():
    st = CountingStore()
    w = _svc(st)
    run(w.credit(tenant="t", fund="cash", amount=10_000, source="c"))
    run(w.spend(tenant="t", net=100, source="dup"))
    st.writes = st.cas_calls = st.scans = 0
    out = run(w.spend(tenant="t", net=100, source="dup"))
    assert out["idempotent"] is True
    assert (st.writes, st.cas_calls, st.scans) == (0, 0, 0)


def test_reserve_and_confirm_never_scan():
    st = CountingStore()
    w = _svc(st)
    run(w.credit(tenant="t", fund="cash", amount=10_000, source="c"))
    st.scans = 0
    h = run(w.reserve(tenant="t", net=100, source="r"))
    run(w.confirm(hold_id=h["hold_id"]))
    assert st.scans == 0, "the hold path scanned the datastore"


def test_the_sweepers_may_scan_they_are_cron_not_the_hot_path():
    st = CountingStore()
    w = _svc(st)
    run(w.credit(tenant="t", fund="cash", amount=10_000, source="c"))
    st.scans = 0
    run(w.sweep_holds())
    assert st.scans >= 1          # documented and deliberate


def test_every_settings_field_is_consumed_somewhere():
    from app.config import Settings

    root = pathlib.Path(__file__).resolve().parents[2]
    files = [p for p in root.rglob("*") if p.is_file() and p.suffix in (".py", ".toml", "")
             and "tests" not in p.parts and ".venv" not in p.parts and "__pycache__" not in p.parts]
    lines: list[str] = []
    for p in files:
        try:
            lines.extend(p.read_text(encoding="utf-8").splitlines())
        except (UnicodeDecodeError, OSError):
            continue
    unused = []
    for name in Settings.model_fields:
        decl = re.compile(rf"^\s*{re.escape(name)}\s*:")
        if not [ln for ln in lines if re.search(rf"\b{re.escape(name)}\b", ln) and not decl.match(ln)]:
            unused.append(name)
    assert not unused, f"config knobs declared but never consumed: {unused}"


def test_wallet_funds_parsing_fails_fast_on_nonsense():
    import os

    from app.config import Settings
    from app.ports.errors import ConfigError

    import pytest

    base = dict(INTERNAL_SERVICE_TOKEN="t" * 40)
    for bad in ("promo:1", "promo:1:maybe", "promo:x:taxed", "", "a:1:taxed,a:2:taxed"):
        s = Settings(**base, WALLET_FUNDS=bad)
        with pytest.raises(ConfigError):
            _ = s.wallet_funds


def test_wallet_funds_are_ordered_by_priority_not_by_declaration():
    from app.config import Settings

    s = Settings(INTERNAL_SERVICE_TOKEN="t" * 40, WALLET_FUNDS="cash:9:taxed,gift:1:untaxed")
    assert [f[0] for f in s.wallet_funds] == ["gift", "cash"]


def test_a_single_fund_wallet_is_valid_config():
    from app.config import Settings

    s = Settings(INTERNAL_SERVICE_TOKEN="t" * 40, WALLET_FUNDS="credits:1:untaxed")
    assert s.wallet_funds == (("credits", 1, False),)
