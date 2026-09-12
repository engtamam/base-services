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

    async def cas(self, c, k, expected_version, v):
        ok = await super().cas(c, k, expected_version, v)
        if ok:                                  # a successful cas is a durable write, like put
            self.writes += 1
        return ok

    async def scan_all(self, c, limit=100):
        self.reads += 1
        return await super().scan_all(c, limit)


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
    # The consent record, one append-only proof, and the proof's per-subject sequence counter.
    # The counter is what makes two proofs written in the same clock tick come back in the order
    # they were written (see ConsentStore.add_proof). A consent grant is a rare, deliberate,
    # auditable event — not a hot path — and the order of the evidence is worth one more write.
    assert st.writes == 3


def test_proofs_written_in_one_clock_tick_come_back_in_write_order():
    """The regulator's question is "did they consent before we processed?" — order is the answer.

    With a frozen clock every proof shares an `at`, so ordering falls to the datastore's `id`
    tie-break. A random uuid would make that arbitrary, and a withdrawal could sort before the
    grant it followed.
    """
    st, cache = CountingStore(), MemoryCache()
    svc = _svc(st, cache)
    for _ in range(5):
        run(svc.grant(tenant="t1", subject="u1", purpose="m", notice_version="v1"))
        run(svc.withdraw(tenant="t1", subject="u1", purpose="m"))

    proofs = run(svc.history(tenant="t1", subject="u1"))["proofs"]
    assert [p["action"] for p in proofs] == ["granted", "withdrawn"] * 5
    assert len({p["at"] for p in proofs}) == 1, "the clock did not actually tie; test proves nothing"


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
