"""Security MEDIUM remediation: cross-tenant replay/cancel audit tenant (job-016)."""
from __future__ import annotations

import asyncio

import pytest


def run(c):
    return asyncio.run(c)


class _Clock:
    def now(self):
        return 1_700_000_000.0


class _Job:
    def __init__(self, status, tenant="acme"):
        self.id = "j1"; self.tenant = tenant; self.status = status; self.attempt = 1
        self.error = "boom"; self.next_run_at = 0.0; self.updated = 0.0; self.unique_key = ""


class _Jobs:
    def __init__(self, job):
        self._job = job
    async def get(self, _id):
        return self._job
    async def save(self, _j):
        pass
    async def index_due(self, _j):
        pass
    async def unindex_due(self, _id):
        pass
    async def unindex_leased(self, _id):
        pass
    async def unique_clear(self, _k):
        pass


def _svc(job):
    from app.core.job_service import JobService
    s = JobService.__new__(JobService)
    s._jobs = _Jobs(job)
    s._clock = _Clock()
    s._owns = lambda j, tenant, is_operator: True
    s._scoped = lambda t, k: f"{t}:{k}"
    return s


def test_replay_returns_the_jobs_tenant():
    from app.core.models import DEAD
    svc = _svc(_Job(DEAD, tenant="shop_a"))
    res = run(svc.replay("j1", tenant=None, is_operator=True))
    assert res["ok"] and res["tenant"] == "shop_a"      # operator cross-tenant: audit names the job's tenant


def test_cancel_returns_the_jobs_tenant():
    from app.core.models import ACTIVE
    status = next(iter(ACTIVE)) if isinstance(ACTIVE, (set, frozenset, list, tuple)) else ACTIVE
    svc = _svc(_Job(status, tenant="shop_b"))
    res = run(svc.cancel("j1", tenant=None, is_operator=True))
    assert res["ok"] and res["tenant"] == "shop_b"
