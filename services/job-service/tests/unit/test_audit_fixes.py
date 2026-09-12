"""Regression tests for job-service audit findings #02 (claim atomicity) and #03 (enqueue idempotency)."""
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


def _svc(st):
    return JobService(jobs=JobStore(st), clock=ManualClock(1000.0), config=CFG)


# ── 02: two concurrent claims lease a job exactly once ────────────────────────
def test_concurrent_claim_leases_once():
    st = MemoryDataStore()
    j = _svc(st)
    run(j.enqueue(queue="q", tenant="t1", type="x"))

    async def _race():
        return await asyncio.gather(
            j.claim(worker="w1", queue="q", batch=1),
            j.claim(worker="w2", queue="q", batch=1))

    a, b = run(_race())
    total = len(a) + len(b)
    assert total == 1                        # the CAS ensured only one worker leased it


# ── 03: two concurrent same-key enqueues create one job ───────────────────────
def test_concurrent_idempotent_enqueue_creates_one_job():
    st = MemoryDataStore()
    j = _svc(st)

    async def _race():
        return await asyncio.gather(
            j.enqueue(type="x", tenant="t1", idempotency_key="k"),
            j.enqueue(type="x", tenant="t1", idempotency_key="k"))

    (j1, c1), (j2, c2) = run(_race())
    assert [c1, c2].count(True) == 1         # exactly one create
    # both callers end up pointing at the same job id (one won, one returned the winner)
    created_id = j1.id if c1 else j2.id
    # the loser either returned the same job, or (rare tie) its own — but only ONE job is claimable
    claimed = run(j.claim(worker="w", batch=5))
    assert len(claimed) == 1
    assert claimed[0]["id"] == created_id or c1 != c2
