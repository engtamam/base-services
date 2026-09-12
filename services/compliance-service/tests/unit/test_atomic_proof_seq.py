"""Regression: ConsentStore.add_proof allocates a per-subject sequence atomically (safety:atomic-cas).

Two proofs for one subject written concurrently must get DISTINCT, monotonic sequence prefixes, or
the tamper-evident order a regulator relies on (grant-before-withdraw) forks.
"""
from __future__ import annotations

import asyncio

from app.adapters.datastore.memory import MemoryDataStore
from app.core.stores import ConsentStore
from app.core.models import ConsentProof


def run(c):
    return asyncio.run(c)


class _YieldStore(MemoryDataStore):
    async def get(self, c, k):
        await asyncio.sleep(0)
        return await super().get(c, k)


def _proof(pid, action):
    return ConsentProof(id=pid, tenant="t1", subject="u1", purpose="marketing", action=action)


def test_concurrent_proofs_get_distinct_sequences():
    store = _YieldStore()
    cs = ConsentStore(store)

    async def _race():
        return await asyncio.gather(
            cs.add_proof(_proof("a", "granted")),
            cs.add_proof(_proof("b", "withdrawn")),
            cs.add_proof(_proof("c", "granted")))

    proofs = run(_race())
    seqs = [int(p.id.split("::")[0]) for p in proofs]
    assert len(set(seqs)) == 3                     # three distinct sequence prefixes (pre-fix: collided)
    assert sorted(seqs) == [1, 2, 3]               # monotonic, no gaps
