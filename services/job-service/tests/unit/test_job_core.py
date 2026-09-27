"""Core queue: atomic claim, fencing, lease-expiry redelivery, retry/DLQ, idempotency, fairness."""
from __future__ import annotations

import asyncio

import pytest

from app.adapters.clock.manual import ManualClock
from app.adapters.cron.builtin import BuiltinCron
from app.adapters.datastore.memory import MemoryDataStore
from app.core.job_service import JobService
from app.core.models import DEAD, DONE, LEASED, QUEUED
from app.core.scheduler import Scheduler
from app.core.stores import JobStore, ScheduleStore

CFG = dict(lease_seconds=60, claim_batch_max=50, heartbeat_extend=30, retry_base_seconds=10,
           retry_max_seconds=3600, retry_max_attempts=3, retry_jitter=True, unique_ttl=86400,
           fairness=True, list_limit=100)


def run(c):
    return asyncio.run(c)


def _svc(clock=None):
    st = MemoryDataStore()
    return JobService(jobs=JobStore(st), clock=clock or ManualClock(1000.0), config=CFG), st


# ── enqueue idempotency / uniqueness ──────────────────────────────────────────
def test_idempotency_key_returns_same_job():
    svc, _ = _svc()
    a, c1 = run(svc.enqueue(type="x", idempotency_key="k"))
    b, c2 = run(svc.enqueue(type="x", idempotency_key="k"))
    assert c1 is True and c2 is False and a.id == b.id


def test_unique_key_refuses_second_pending():
    svc, _ = _svc()
    a, c1 = run(svc.enqueue(type="sweep", unique_key="dunning"))
    b, c2 = run(svc.enqueue(type="sweep", unique_key="dunning"))
    assert c2 is False and a.id == b.id     # no stacking 10 identical sweeps


def test_unique_slot_frees_after_completion():
    svc, _ = _svc()
    a, _ = run(svc.enqueue(type="sweep", unique_key="u"))
    claimed = run(svc.claim(worker="w", batch=1))
    run(svc.ack(claimed[0]["id"], claimed[0]["lease_token"]))
    b, created = run(svc.enqueue(type="sweep", unique_key="u"))
    assert created is True and b.id != a.id   # a new run may be enqueued once the old is done


# ── FIN job-001: dedupe keys are tenant-scoped (no cross-tenant collision/suppression) ─────────────
def test_unique_key_does_not_collide_across_tenants():
    svc, _ = _svc()
    a, ca = run(svc.enqueue(tenant="tenantA", type="report", unique_key="daily-report"))
    b, cb = run(svc.enqueue(tenant="tenantB", type="report", unique_key="daily-report"))
    assert ca is True and cb is True          # both created — B is NOT suppressed by A's key
    assert a.id != b.id and a.tenant == "tenantA" and b.tenant == "tenantB"  # no id leak across tenants


def test_idempotency_key_does_not_collide_across_tenants():
    svc, _ = _svc()
    a, ca = run(svc.enqueue(tenant="tenantA", type="x", idempotency_key="k1"))
    b, cb = run(svc.enqueue(tenant="tenantB", type="x", idempotency_key="k1"))
    assert ca is True and cb is True and a.id != b.id


def test_same_tenant_unique_key_still_dedupes():
    # scoping must NOT weaken same-tenant dedup.
    svc, _ = _svc()
    a, ca = run(svc.enqueue(tenant="tenantA", type="report", unique_key="daily-report"))
    b, cb = run(svc.enqueue(tenant="tenantA", type="report", unique_key="daily-report"))
    assert ca is True and cb is False and a.id == b.id


def test_tenant_ambiguity_prefix_cannot_collide():
    # the classic length-prefix case: ("a","b:c") vs ("a:b","c") must not alias.
    svc, _ = _svc()
    a, _ = run(svc.enqueue(tenant="a", type="x", unique_key="b:c"))
    b, cb = run(svc.enqueue(tenant="a:b", type="x", unique_key="c"))
    assert cb is True and a.id != b.id


# ── atomic claim ──────────────────────────────────────────────────────────────
def test_two_workers_never_get_the_same_job():
    svc, _ = _svc()
    for _ in range(20):
        run(svc.enqueue(type="x"))
    a = run(svc.claim(worker="w1", batch=20))
    b = run(svc.claim(worker="w2", batch=20))
    ida = {j["id"] for j in a}
    idb = {j["id"] for j in b}
    assert ida.isdisjoint(idb) and len(ida) + len(idb) == 20


def test_claimed_job_is_invisible_until_lease_expires():
    svc, _ = _svc()
    run(svc.enqueue(type="x"))
    run(svc.claim(worker="w1", batch=1))
    assert run(svc.claim(worker="w2", batch=1)) == []     # leased → not claimable


# ── fencing token ─────────────────────────────────────────────────────────────
def test_ack_with_stale_token_is_rejected():
    svc, _ = _svc()
    run(svc.enqueue(type="x"))
    c = run(svc.claim(worker="w", batch=1))
    assert run(svc.ack(c[0]["id"], "wrong-token"))["ok"] is False
    assert run(svc.ack(c[0]["id"], c[0]["lease_token"]))["ok"] is True


def test_stale_worker_cannot_ack_after_reclaim():
    clk = ManualClock(1000.0)
    svc, _ = _svc(clk)
    run(svc.enqueue(type="x"))
    c1 = run(svc.claim(worker="slow", batch=1))          # worker w1 leases
    clk.advance(120)                                      # its lease expires
    run(svc.reclaim())                                    # returned to the queue
    c2 = run(svc.claim(worker="fast", batch=1))          # w2 leases it fresh
    # the slow worker finally finishes and tries to ack with its OLD token → refused
    assert run(svc.ack(c1[0]["id"], c1[0]["lease_token"]))["ok"] is False
    assert run(svc.ack(c2[0]["id"], c2[0]["lease_token"]))["ok"] is True


# ── lease expiry (crash) ──────────────────────────────────────────────────────
def test_crashed_worker_job_is_redelivered_not_lost():
    clk = ManualClock(1000.0)
    svc, _ = _svc(clk)
    job, _ = run(svc.enqueue(type="x"))
    run(svc.claim(worker="w1", batch=1))                 # claimed, never acked (crash)
    assert run(svc.claim(worker="w2", batch=1)) == []    # invisible during lease
    clk.advance(120)
    assert run(svc.reclaim())["reclaimed"] == 1
    again = run(svc.claim(worker="w2", batch=1))         # immediately claimable again
    assert len(again) == 1 and again[0]["id"] == job.id


def test_heartbeat_extends_the_lease():
    clk = ManualClock(1000.0)
    svc, _ = _svc(clk)
    run(svc.enqueue(type="x"))
    c = run(svc.claim(worker="w", batch=1))
    clk.advance(50)
    run(svc.heartbeat(c[0]["id"], c[0]["lease_token"]))  # +30 → expires at ~1080
    clk.advance(20)                                       # now 1070, still held
    assert run(svc.reclaim())["reclaimed"] == 0


def test_poison_job_that_crashes_workers_still_reaches_dlq():
    clk = ManualClock(1000.0)
    svc, _ = _svc(clk)
    job, _ = run(svc.enqueue(type="x"))                  # max_attempts=3
    for _ in range(5):
        run(svc.claim(worker="w", batch=1))
        clk.advance(120)
        run(svc.reclaim())
    assert run(svc.get(job.id)).status == DEAD           # not an infinite crash loop


# ── retry / DLQ / replay ──────────────────────────────────────────────────────
def test_nack_retries_with_backoff_then_dead_letters():
    clk = ManualClock(1000.0)
    svc, _ = _svc(clk)
    job, _ = run(svc.enqueue(type="x"))
    for _ in range(3):
        clk.advance(10000)
        c = run(svc.claim(worker="w", batch=1))
        assert c, "job should be re-claimable after backoff"
        out = run(svc.nack(c[0]["id"], c[0]["lease_token"], "boom"))
    assert out["status"] == DEAD
    assert run(svc.replay(job.id))["status"] == QUEUED
    assert run(svc.get(job.id)).status == QUEUED


def test_nack_schedules_a_jittered_future_retry():
    clk = ManualClock(1000.0)
    svc, _ = _svc(clk)
    run(svc.enqueue(type="x"))
    c = run(svc.claim(worker="w", batch=1))
    out = run(svc.nack(c[0]["id"], c[0]["lease_token"], "boom"))

    # Full jitter picks uniformly in (0, ceiling] where ceiling = base * 2**attempt, capped.
    # After the first nack `attempt` is 1, so the ceiling is 20 — not 10. The jitter is seeded
    # on the job id, which is random, so a bound of 10 fails whenever the draw lands above it.
    ceiling = min(CFG["retry_base_seconds"] * 2 ** out["attempt"], CFG["retry_max_seconds"])
    assert out["status"] == QUEUED
    assert 0 < out["retry_in"] <= ceiling
    assert run(svc.claim(worker="w", batch=1)) == []     # not due yet (backoff)


def test_backoff_never_exceeds_its_ceiling_for_any_job_id():
    """The bound must hold for every seed, not for the one the last test happened to draw."""
    from app.core import backoff

    for attempt in range(3):
        ceiling = min(10 * 2 ** attempt, 3_600)
        for i in range(500):
            d = backoff.next_delay(attempt, base_seconds=10, max_seconds=3_600,
                                   max_attempts=5, seed=f"job-{i}")
            assert 0 <= d <= ceiling


# ── priority + fairness ───────────────────────────────────────────────────────
def test_priority_claimed_first():
    svc, _ = _svc()
    run(svc.enqueue(type="low", priority=0))
    run(svc.enqueue(type="high", priority=10))
    first = run(svc.claim(worker="w", batch=1))
    assert first[0]["type"] == "high"


def test_fairness_round_robins_across_tenants():
    svc, _ = _svc()
    for _ in range(5):
        run(svc.enqueue(type="x", tenant="loud"))     # one tenant floods
    run(svc.enqueue(type="x", tenant="quiet"))         # another has a single job
    claimed = run(svc.claim(worker="w", batch=2))
    tenants = {j["tenant"] for j in claimed}
    assert "quiet" in tenants                            # not starved behind loud's flood


# ── cancel ────────────────────────────────────────────────────────────────────
def test_cancel_removes_from_queue():
    svc, _ = _svc()
    job, _ = run(svc.enqueue(type="x"))
    assert run(svc.cancel(job.id))["ok"] is True
    assert run(svc.claim(worker="w", batch=5)) == []


# ── scheduler ─────────────────────────────────────────────────────────────────
def _sched(clk):
    st = MemoryDataStore()
    js = JobService(jobs=JobStore(st), clock=clk, config=CFG)
    return Scheduler(schedules=ScheduleStore(st), jobs=js, clock=clk, cron=BuiltinCron()), js


def test_schedule_tick_enqueues_when_due():
    clk = ManualClock(1000.0)
    sch, js = _sched(clk)
    s = run(sch.create(name="n", cron="*/5 * * * *", type="sweep"))
    assert run(sch.tick())["fired"] == 0                 # not due yet
    clk.set(s.next_run_at + 1)
    assert run(sch.tick())["fired"] == 1                 # fired
    assert len(run(js.claim(worker="w", batch=5))) == 1


def test_singleton_skips_when_prior_run_still_active():
    clk = ManualClock(1000.0)
    sch, js = _sched(clk)
    s = run(sch.create(name="n", cron="* * * * *", type="slow", singleton=True))
    clk.set(s.next_run_at + 1)
    run(sch.tick())                                       # fires run #1 (still queued)
    s2 = run(sch.get(s.id))
    clk.set(s2.next_run_at + 1)
    out = run(sch.tick())                                 # run #1 still active → skip
    assert out["fired"] == 0 and out["skipped"] == 1


def test_non_singleton_can_overlap():
    clk = ManualClock(1000.0)
    sch, js = _sched(clk)
    s = run(sch.create(name="n", cron="* * * * *", type="x", singleton=False))
    clk.set(s.next_run_at + 1)
    run(sch.tick())
    s2 = run(sch.get(s.id))
    clk.set(s2.next_run_at + 1)
    assert run(sch.tick())["fired"] == 1                 # overlaps allowed


# ── cross-tenant IDOR on the by-id admin actions ────────────────────────────
def test_cancel_and_replay_reject_another_tenants_job():
    from app.core.models import DEAD as _DEAD
    svc, st = _svc()
    job, _ = run(svc.enqueue(type="x", tenant="shop_a"))
    # a tenant-scoped caller from shop_b cannot cancel shop_a's job
    out = run(svc.cancel(job.id, tenant="shop_b", is_operator=False))
    assert out["ok"] is False and out["reason"] == "not_found"
    assert run(svc.get(job.id)).status != "canceled"          # untouched
    # nor replay it (drive it to DEAD first via the store, then attempt cross-tenant replay)
    j = run(svc.get(job.id)); j.status = _DEAD; run(st.put("jobs", j.id, j.to_dict()))
    r = run(svc.replay(job.id, tenant="shop_b", is_operator=False))
    assert r["ok"] is False and r["reason"] == "not_found"
    # the owning tenant CAN
    assert run(svc.replay(job.id, tenant="shop_a", is_operator=False))["ok"] is True
