"""Non-security MEDIUM remediation (job-service):
  006  dispatch_batch delivers with bounded concurrency, not strictly sequentially.
  008  the scheduler catches up missed occurrences instead of silently dropping them.
  009  counts() always populates every status with 0 and marks truncation explicitly.
  010  _emit telemeters a publish failure instead of swallowing it silently.
  014  a target whose host is no longer allow-listed is skipped at delivery, not POSTed to.
"""
from __future__ import annotations

import asyncio

from app.adapters.clock.manual import ManualClock
from app.adapters.datastore.memory import MemoryDataStore
from app.core.dispatcher import Dispatcher
from app.core.job_service import JobService
from app.core.stores import DispatchTargetStore, JobStore

CFG_JOBS = dict(lease_seconds=60, claim_batch_max=50, heartbeat_extend=30, retry_base_seconds=10,
                retry_max_seconds=3600, retry_max_attempts=3, retry_jitter=False, unique_ttl=86400,
                fairness=True, list_limit=100)
SECRET = "s" * 40


def run(c):
    return asyncio.run(c)


# ── 010: _emit telemeters a publish failure ───────────────────────────────────
class _BrokenBus:
    async def publish(self, event, payload):
        raise RuntimeError("bus down")


class _Telemetry:
    def __init__(self): self.events = []
    def event(self, name, data): self.events.append((name, data))


def test_emit_telemeters_a_publish_failure():
    tel = _Telemetry()
    svc = JobService(jobs=JobStore(MemoryDataStore()), clock=ManualClock(1.0), config={},
                     event_bus=_BrokenBus(), telemetry=tel)
    run(svc._emit("job.enqueued", None, {"x": 1}))        # must not raise
    assert any(n == "event.publish_failed" for n, _ in tel.events)


# ── 009: counts() shape is stable and truncation is visible ───────────────────
def test_counts_populates_all_statuses_with_zero():
    svc_store = JobStore(MemoryDataStore())
    out = run(svc_store.counts(tenant="t-empty"))
    assert out == {"queued": 0, "leased": 0, "done": 0, "dead": 0, "canceled": 0}


def test_counts_marks_truncation_explicitly():
    st = MemoryDataStore()
    store = JobStore(st)
    jobs = JobService(jobs=store, clock=ManualClock(1.0), config=CFG_JOBS)
    for _ in range(5):
        run(jobs.enqueue(queue="q", tenant="t1", type="x", payload={}))
    out = run(store.counts(tenant="t1", cap=2))
    assert out["queued"] == 2                              # reported at the cap, not the true 5
    assert out.get("_truncated") == ["queued"]            # and the cap is visible


# ── 014: a de-listed target host is skipped at delivery ───────────────────────
class _Poster:
    def __init__(self): self.calls = []
    async def __call__(self, url, body, headers, timeout):
        self.calls.append(url)
        return 200


def _wire(poster, hosts):
    st = MemoryDataStore()
    clock = ManualClock(1000.0)
    jobs = JobService(jobs=JobStore(st), clock=clock, config=CFG_JOBS)
    cfg = {"enabled": True, "batch": 20, "timeout": 5, "allowed_hosts": frozenset(hosts),
           "signing_secret": SECRET, "concurrency": 4}
    disp = Dispatcher(targets=DispatchTargetStore(st), jobs=jobs, clock=clock, config=cfg,
                      poster=poster, headers_for=lambda host: {"X-Internal-Token": "tok"})
    return disp, jobs, st


def test_target_delisted_after_register_is_skipped_at_delivery():
    poster = _Poster()
    disp, jobs, _ = _wire(poster, hosts=("peer",))
    run(disp.register(queue="q", url="http://peer/hook"))
    run(jobs.enqueue(queue="q", tenant="t1", type="x", payload={}))
    # the host leaves the allowlist AFTER registration
    disp._cfg["allowed_hosts"] = frozenset()
    res = run(disp.dispatch_batch())
    assert res["skipped_targets"] == 1 and res["dispatched"] == 0
    assert poster.calls == []                              # never POSTed to the de-listed host


def test_allowlisted_target_still_delivers():
    poster = _Poster()
    disp, jobs, _ = _wire(poster, hosts=("peer",))
    run(disp.register(queue="q", url="http://peer/hook"))
    run(jobs.enqueue(queue="q", tenant="t1", type="x", payload={}))
    res = run(disp.dispatch_batch())
    assert res["dispatched"] == 1 and res["skipped_targets"] == 0
    assert poster.calls == ["http://peer/hook"]


# ── 008: the scheduler catches up missed occurrences ──────────────────────────
def test_scheduler_catches_up_missed_occurrences():
    from app.core import cron as cron_mod
    from app.core.scheduler import Scheduler
    from app.core.stores import ScheduleStore

    st = MemoryDataStore()
    clock = ManualClock(0.0)
    jobs = JobService(jobs=JobStore(st), clock=clock, config=CFG_JOBS)
    sched = Scheduler(schedules=ScheduleStore(st), jobs=jobs, clock=clock, cron=cron_mod,
                      singleton_default=False, catch_up_limit=100)
    # an every-minute schedule created at t=0
    s = run(sched.create(name="m", cron="* * * * *", queue="q", tenant="t1", type="x"))
    # jump the clock forward 5 minutes without ticking — 5 occurrences were missed
    clock.set(s.next_run_at + 5 * 60)
    res = run(sched.tick())
    assert res["fired"] >= 5                               # every missed run fired, not just one
