"""Round-2 fixes (job-service). Maps to docs/audits/round2/job-service.json."""
from __future__ import annotations

import asyncio

from app.adapters.clock.manual import ManualClock
from app.adapters.datastore.memory import MemoryDataStore
from app.core.job_service import JobService
from app.core.stores import JobStore

CFG = dict(lease_seconds=60, claim_batch_max=50, heartbeat_extend=60, retry_base_seconds=10,
           retry_max_seconds=3600, retry_max_attempts=5, retry_jitter=True, unique_ttl=86400,
           fairness=True, list_limit=100)


def run(c):
    return asyncio.run(c)


class _RaceStore(MemoryDataStore):
    """Simulate a concurrent ack landing BETWEEN reclaim's read of a leased job and its write: the
    first time reclaim reads the target job (still LEASED), we finish it (DONE, version bumped) in the
    store but hand reclaim the stale LEASED snapshot — the exact window job-r2-01 describes."""
    def __init__(self):
        super().__init__()
        self.target = None
        self._raced = False

    async def get(self, collection, key):
        row = await super().get(collection, key)
        if (collection == "jobs" and key == self.target and not self._raced
                and row and row.get("status") == "leased"):
            self._raced = True
            done = {**row, "status": "done", "lease_token": "",
                    "version": int(row.get("version", 0)) + 1}
            await super().put(collection, key, done)   # concurrent ack commits
            return dict(row)                            # reclaim still sees the pre-ack LEASED snapshot
        return row


# r2-01 — reclaim must NOT resurrect a job a concurrent ack already finished (no re-execution).
def test_reclaim_does_not_resurrect_a_concurrently_finished_job():
    clk = ManualClock(1000.0)
    st = _RaceStore()
    svc = JobService(jobs=JobStore(st), clock=clk, config=CFG)
    job, _ = run(svc.enqueue(type="x"))
    claimed = run(svc.claim(worker="w1", batch=1))
    assert claimed and claimed[0]["status"] == "leased"
    st.target = job.id
    clk.set(5000.0)                                       # lease long expired

    out = run(svc.reclaim())
    assert out["reclaimed"] == 0                          # the CAS lost → reclaim skipped it
    assert run(svc.get(job.id)).status == "done"          # stays DONE, not resurrected to QUEUED


# r2-02 — a tenant's /stats counts via bounded INDEXED finds, never a scan of the whole job table.
def test_tenant_stats_does_not_scan_the_job_table():
    class _Counting(MemoryDataStore):
        def __init__(self):
            super().__init__()
            self.scans = {}
        async def scan_all(self, c, limit=100):
            self.scans[c] = self.scans.get(c, 0) + 1
            return await super().scan_all(c, limit)

    st = _Counting()
    svc = JobService(jobs=JobStore(st), clock=ManualClock(1000.0), config=CFG)
    for i in range(10):
        run(svc.enqueue(queue="q", tenant="t1", type="x"))
    st.scans = {}
    out = run(svc.stats(tenant="t1"))
    assert st.scans.get("jobs", 0) == 0            # no whole-job-table scan for a tenant caller
    assert out["by_status"].get("queued", 0) == 10


# cas-create — enqueue must CREATE the job via the insert-if-absent CAS, so the row lands
# version-stamped and the FIRST version-pinned claim CAS wins on the strict postgres adapter. A
# put-created (versionless) row made that first claim a no-op on postgres — jobs never claimed in
# prod while the lax memory cas kept tests green. Asserting the stamped version catches it on memory.
def test_enqueue_creates_version_stamped_row_and_first_claim_cas_wins():
    st = MemoryDataStore()
    svc = JobService(jobs=JobStore(st), clock=ManualClock(1000.0), config=CFG)
    job, created = run(svc.enqueue(type="x"))
    assert created
    assert int(run(st.get("jobs", job.id)).get("version", 0)) == 1   # cas-created, not put (would be 0)
    claimed = run(svc.claim(worker="w1", batch=1))
    assert claimed and claimed[0]["id"] == job.id                    # version-pinned QUEUED->LEASED won
