"""Non-functional guards (per memory `phase-doc-rules`).

A queue is mutable state, so 'cache to zero reads' doesn't apply. The correct hot-path
invariants: enqueue is 1 write + 1 index (no scan); claim reads the small due-index
(bounded by batch), never the whole job table.
"""
from __future__ import annotations

import asyncio
import pathlib
import re

from app.adapters.clock.manual import ManualClock
from app.adapters.datastore.memory import MemoryDataStore
from app.core.job_service import JobService
from app.core.stores import JobStore

CFG = dict(lease_seconds=60, claim_batch_max=50, heartbeat_extend=60, retry_base_seconds=10,
           retry_max_seconds=3600, retry_max_attempts=5, retry_jitter=True, unique_ttl=86400,
           fairness=True, list_limit=100)


def run(c):
    return asyncio.run(c)


class CountingStore(MemoryDataStore):
    def __init__(self):
        super().__init__()
        self.writes = self.reads = 0
        self.scans: dict[str, int] = {}

    async def put(self, c, k, v):
        self.writes += 1
        return await super().put(c, k, v)

    async def cas(self, c, k, expected, v):
        ok = await super().cas(c, k, expected, v)
        if ok:
            self.writes += 1      # a successful cas is a write (enqueue reserve + claim use it)
        return ok

    async def get(self, c, k):
        self.reads += 1
        return await super().get(c, k)

    async def scan_all(self, c, limit=100):
        self.scans[c] = self.scans.get(c, 0) + 1
        return await super().scan_all(c, limit)


def _svc(st):
    return JobService(jobs=JobStore(st), clock=ManualClock(1000.0), config=CFG)


def test_enqueue_is_one_job_write_plus_index_no_scan():
    st = CountingStore()
    j = _svc(st)
    st.writes = 0
    st.scans = {}
    run(j.enqueue(queue="q", tenant="t1", type="x"))
    assert st.writes == 2, f"enqueue did {st.writes} writes (want job + due-index)"
    assert "jobs" not in st.scans, "enqueue scanned the job table"


def test_enqueue_with_keys_writes_index_entries_but_still_no_scan():
    st = CountingStore()
    j = _svc(st)
    st.writes = 0
    st.scans = {}
    run(j.enqueue(type="x", idempotency_key="k", unique_key="u"))
    assert st.writes == 4          # job + due + 2 unique-index rows
    assert "jobs" not in st.scans


def test_claim_reads_due_index_not_the_job_table():
    st = CountingStore()
    j = _svc(st)
    for i in range(200):
        run(j.enqueue(queue="q", tenant=f"t{i % 5}", type="x"))
    st.scans = {}
    run(j.claim(worker="w", batch=10))
    assert st.scans.get("jobs", 0) == 0, "claim scanned the whole job table"
    assert st.scans.get("jobs_due", 0) >= 1          # it read the small index instead


def test_claim_is_bounded_by_batch_not_table_size():
    st = MemoryDataStore()
    j = _svc(st)
    for _ in range(200):
        run(j.enqueue(type="x"))
    assert len(run(j.claim(worker="w", batch=10))) == 10       # only batch, not 200
    assert len(run(j.claim(worker="w", batch=1000))) == 50     # capped at claim_batch_max


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
