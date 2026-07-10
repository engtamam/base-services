"""Non-functional guards (per memory `phase-doc-rules`).

Credential resolution runs before every outbound provider call. It must serve from cache
with zero datastore reads, and N concurrent expiring calls must trigger exactly ONE refresh
(providers rotate refresh tokens — a concurrent double-refresh permanently breaks a
connection, so single-flight is correctness, not performance).
"""
from __future__ import annotations

import asyncio
import pathlib
import re

from app.adapters.cache.memory import MemoryCache
from app.adapters.clock.manual import ManualClock
from app.adapters.datastore.memory import MemoryDataStore
from app.adapters.provider.recording import RecordingConnector
from app.core.models import Connection
from app.core.stores import ConnectionStore
from app.core.token_manager import TokenManager
from app.ports.connector_provider import Tokens

SEAL = "k" * 32


def run(c):
    return asyncio.run(c)


class CountingStore(MemoryDataStore):
    def __init__(self):
        super().__init__()
        self.reads = self.writes = 0

    async def get(self, c, k):
        self.reads += 1
        return await super().get(c, k)

    async def put(self, c, k, v):
        self.writes += 1
        return await super().put(c, k, v)

    async def scan_all(self, c, limit=100):
        self.reads += 1
        return await super().scan_all(c, limit)


def _env(expires_in=3600):
    st = CountingStore()
    clk = ManualClock(1000.0)
    prov = RecordingConnector()
    conns = ConnectionStore(st)
    tm = TokenManager(connections=conns, provider=prov, clock=clk, seal_key=SEAL,
                      cache=MemoryCache(), cache_ttl=300, refresh_skew=60)
    conn = Connection(id="cn_1", tenant="t1", provider="rec")
    run(conns.save(conn))
    run(tm.store_tokens(conn, Tokens(access_token="A1", refresh_token="R1", expires_in=expires_in),
                        invalidate=True))
    return tm, st, prov, clk


def test_repeated_credential_resolution_does_zero_reads_after_warm():
    tm, st, _, _ = _env()
    run(tm.credentials("t1", "cn_1", {}))       # warm
    st.reads = 0
    for _ in range(100):
        run(tm.credentials("t1", "cn_1", {}))
    assert st.reads == 0, f"hot path did {st.reads} datastore reads"


def test_n_concurrent_expiring_calls_do_exactly_one_refresh():
    tm, _, prov, clk = _env()
    clk.advance(3600 - 30)                       # inside the refresh skew

    async def race():
        return await asyncio.gather(*[tm.credentials("t1", "cn_1", {}) for _ in range(25)])

    tokens = asyncio.run(race())
    assert prov.refresh_calls == 1, f"{prov.refresh_calls} concurrent refreshes — rotation race"
    assert len(set(tokens)) == 1


def test_cache_outage_never_breaks_credential_resolution():
    class BrokenCache:
        driver = "broken"

        async def get(self, key):
            raise RuntimeError("down")

        async def set(self, key, value, ttl_seconds=None):
            raise RuntimeError("down")

    st = MemoryDataStore()
    conns = ConnectionStore(st)
    tm = TokenManager(connections=conns, provider=RecordingConnector(), clock=ManualClock(1000.0),
                      seal_key=SEAL, cache=BrokenCache())
    conn = Connection(id="cn_1", tenant="t1", provider="rec")
    run(conns.save(conn))
    run(tm.store_tokens(conn, Tokens(access_token="A1", refresh_token="R1", expires_in=3600)))
    assert run(tm.credentials("t1", "cn_1", {})) == "A1"


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
