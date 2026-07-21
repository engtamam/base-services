"""The daily spend ceiling."""
from __future__ import annotations

import asyncio
from dataclasses import dataclass

from app.core.quota import DAY_SECONDS, DailyQuota


def run(c):
    return asyncio.run(c)


@dataclass
class _V:
    allowed: bool
    remaining: int = 0
    retry_after_s: int = 0
    limit: int = 0


class _Limiter:
    """Counts spend per key. Mirrors the real port, including that it floors cost at 1 — which is
    why there is no read-only pre-check in the quota."""
    driver = "fake"
    def __init__(self):
        self.used: dict[str, int] = {}
        self.windows: list[int] = []
    async def check(self, key, *, cost=1, limit=0, window_s=0):
        self.windows.append(window_s)
        cost = max(1, cost)
        used = self.used.get(key, 0)
        if used + cost > limit:
            return _V(allowed=False, remaining=0, retry_after_s=60)
        self.used[key] = used + cost
        return _V(allowed=True, remaining=limit - self.used[key])
    async def refund(self, key, *, cost=1, limit=0, window_s=0):
        self.used[key] = max(0, self.used.get(key, 0) - cost)


def _q(lim, subject=2, tenant=10):
    return DailyQuota(lim, per_subject=subject, per_tenant=tenant)


def test_within_budget_is_allowed():
    assert run(_q(_Limiter()).spend(tenant="acme", subject_ref="sub_a")).allowed


def test_the_window_is_a_day():
    lim = _Limiter()
    run(_q(lim).spend(tenant="acme", subject_ref="sub_a"))
    assert set(lim.windows) == {DAY_SECONDS}


def test_a_subject_exhausting_their_own_budget_is_refused():
    lim = _Limiter(); q = _q(lim, subject=2)
    for _ in range(2):
        run(q.spend(tenant="acme", subject_ref="sub_a"))
    v = run(q.spend(tenant="acme", subject_ref="sub_a"))
    assert not v.allowed and v.scope == "subject"


def test_one_heavy_user_does_not_exhaust_their_colleagues():
    """A single tenant-wide counter makes the quota read as an outage to people who did nothing."""
    lim = _Limiter(); q = _q(lim, subject=2, tenant=10)
    for _ in range(2):
        run(q.spend(tenant="acme", subject_ref="sub_heavy"))
    assert not run(q.spend(tenant="acme", subject_ref="sub_heavy")).allowed
    assert run(q.spend(tenant="acme", subject_ref="sub_colleague")).allowed


def test_the_tenant_ceiling_still_bounds_the_bill():
    lim = _Limiter(); q = _q(lim, subject=1000, tenant=3)
    for i in range(3):
        run(q.spend(tenant="acme", subject_ref=f"sub_{i}"))
    v = run(q.spend(tenant="acme", subject_ref="sub_new"))
    assert not v.allowed and v.scope == "tenant"


def test_another_tenant_has_its_own_budget():
    lim = _Limiter(); q = _q(lim, subject=1000, tenant=1)
    run(q.spend(tenant="acme", subject_ref="sub_a"))
    assert not run(q.spend(tenant="acme", subject_ref="sub_b")).allowed
    assert run(q.spend(tenant="globex", subject_ref="sub_a")).allowed


def test_zero_means_no_ceiling_and_costs_no_round_trip():
    lim = _Limiter()
    q = DailyQuota(lim, per_subject=0, per_tenant=0)
    assert run(q.spend(tenant="acme", subject_ref="sub_a")).allowed
    assert lim.windows == [], "an unconfigured quota should not touch the limiter at all"


def test_an_abstention_is_refunded_so_it_costs_nothing():
    lim = _Limiter(); q = _q(lim, subject=1)
    run(q.spend(tenant="acme", subject_ref="sub_a"))
    run(q.refund(tenant="acme", subject_ref="sub_a"))
    assert run(q.spend(tenant="acme", subject_ref="sub_a")).allowed


def test_a_tenant_refusal_refunds_the_subject_unit():
    """Otherwise a request the tenant ceiling rejected still drains the person's own budget."""
    lim = _Limiter(); q = _q(lim, subject=5, tenant=1)
    run(q.spend(tenant="acme", subject_ref="sub_a"))          # uses tenant's only unit
    before = lim.used["assistant:quota:subject:sub_b"] if "assistant:quota:subject:sub_b" in lim.used else 0
    v = run(q.spend(tenant="acme", subject_ref="sub_b"))
    assert not v.allowed and v.scope == "tenant"
    assert lim.used.get("assistant:quota:subject:sub_b", 0) == before


def test_a_failed_refund_never_fails_the_request():
    class _Broken(_Limiter):
        async def refund(self, *a, **kw): raise RuntimeError("store down")
    q = _q(_Broken(), subject=5)
    run(q.spend(tenant="acme", subject_ref="sub_a"))
    run(q.refund(tenant="acme", subject_ref="sub_a"))   # must not raise
