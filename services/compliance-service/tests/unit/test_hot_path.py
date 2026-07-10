"""Non-functional guards (per memory `phase-doc-rules`).

`consent_check` gates EVERY processing of personal data — it is the hot path. It must serve
from cache with zero datastore reads, and a consent change must invalidate immediately
(a cached "allowed" outliving a withdrawal is a legal violation, not just a stale read).
"""
from __future__ import annotations

import asyncio
import pathlib
import re

from app.adapters.cache.memory import MemoryCache
from app.adapters.clock.manual import ManualClock
from app.adapters.datastore.memory import MemoryDataStore
from app.core.consent_service import ConsentService
from app.core.stores import ConsentStore


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

    async def list(self, c, limit=100):
        self.reads += 1
        return await super().list(c, limit)


def _svc(st, cache):
    return ConsentService(store=ConsentStore(st), clock=ManualClock(1_000_000.0),
                          cache=cache, cache_ttl=300)


def test_repeated_consent_check_does_zero_reads_after_warm():
    st, cache = CountingStore(), MemoryCache()
    svc = _svc(st, cache)
    run(svc.grant(tenant="t1", subject="u1", purpose="marketing"))
    run(svc.check(tenant="t1", subject="u1", purpose="marketing"))     # warm
    st.reads = 0
    for _ in range(100):
        run(svc.check(tenant="t1", subject="u1", purpose="marketing"))
    assert st.reads == 0, f"consent check did {st.reads} datastore reads on the hot path"


def test_denied_check_is_also_cached():
    st, cache = CountingStore(), MemoryCache()
    svc = _svc(st, cache)
    run(svc.check(tenant="t1", subject="ghost", purpose="x"))          # warm the DENY
    st.reads = 0
    for _ in range(50):
        assert run(svc.check(tenant="t1", subject="ghost", purpose="x"))["allowed"] is False
    assert st.reads == 0


def test_grant_and_withdraw_bust_the_cache_immediately():
    st, cache = CountingStore(), MemoryCache()
    svc = _svc(st, cache)
    assert run(svc.check(tenant="t1", subject="u1", purpose="m"))["allowed"] is False
    run(svc.grant(tenant="t1", subject="u1", purpose="m"))
    assert run(svc.check(tenant="t1", subject="u1", purpose="m"))["allowed"] is True   # not stale
    run(svc.withdraw(tenant="t1", subject="u1", purpose="m"))
    r = run(svc.check(tenant="t1", subject="u1", purpose="m"))
    assert r["allowed"] is False and r["cached"] is False


def test_grant_writes_consent_plus_one_proof():
    st, cache = CountingStore(), MemoryCache()
    svc = _svc(st, cache)
    st.writes = 0
    run(svc.grant(tenant="t1", subject="u1", purpose="m"))
    assert st.writes == 2      # the consent record + one append-only proof. Nothing else.


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
