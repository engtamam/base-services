"""Push dispatch — the queue-backed delivery worker.

The invariant that matters: push reuses the durable queue, so it inherits every guarantee. A delivery
is a claim → POST → ack/nack; a failed POST is a nack into the SAME backoff/retry/DLQ; the atomic
claim means many dispatchers deliver each job exactly once; each POST carries only its own tenant; and
every POST is signed (integrity + replay) — verifiable by the receiver.
"""
from __future__ import annotations

import asyncio
import json

import pytest

from app.adapters.clock.manual import ManualClock
from app.adapters.datastore.memory import MemoryDataStore
from app.core import push_signing
from app.core.dispatcher import Dispatcher, DispatchError
from app.core.job_service import JobService
from app.core.models import DEAD, DONE, QUEUED
from app.core.stores import DispatchTargetStore, JobStore

CFG_JOBS = dict(lease_seconds=60, claim_batch_max=50, heartbeat_extend=30, retry_base_seconds=10,
                retry_max_seconds=3600, retry_max_attempts=3, retry_jitter=False, unique_ttl=86400,
                fairness=True, list_limit=100)

SECRET = "s" * 40


def run(c):
    return asyncio.run(c)


class _Poster:
    """Captures every POST and returns a scripted status (or raises), so a test can assert the exact
    wire shape and drive success/failure/transport-error."""
    def __init__(self, status=200, boom=False):
        self.status, self.boom, self.calls = status, boom, []

    async def __call__(self, url, body, headers, timeout):
        self.calls.append({"url": url, "body": body, "headers": headers, "timeout": timeout})
        if self.boom:
            raise ConnectionError("peer down")
        return self.status


def _wire(clock=None, poster=None, enabled=True, batch=20, hosts=("peer",)):
    st = MemoryDataStore()
    clock = clock or ManualClock(1000.0)
    jobs = JobService(jobs=JobStore(st), clock=clock, config=CFG_JOBS)
    cfg = {"enabled": enabled, "batch": batch, "timeout": 5,
           "allowed_hosts": frozenset(hosts), "signing_secret": SECRET, "max_skew_seconds": 300}
    disp = Dispatcher(targets=DispatchTargetStore(st), jobs=jobs, clock=clock, config=cfg,
                      poster=poster or _Poster(), headers_for=lambda host: {"X-Internal-Token": "tok"})
    return disp, jobs, st, clock


# ── target registration: SSRF allowlist (CWE-918) ─────────────────────────────
def test_register_refuses_a_non_allowlisted_host():
    disp, *_ = _wire()
    with pytest.raises(DispatchError, match="not allow-listed"):
        run(disp.register(queue="q", url="http://evil.example/hook"))


def test_register_refuses_a_non_http_scheme_and_missing_host():
    disp, *_ = _wire()
    with pytest.raises(DispatchError):
        run(disp.register(queue="q", url="file:///etc/passwd"))
    with pytest.raises(DispatchError):
        run(disp.register(queue="q", url="http:///nohost"))


def test_register_accepts_an_allowlisted_host_and_replaces():
    disp, _, st, _ = _wire()
    run(disp.register(queue="q", url="http://peer/a"))
    run(disp.register(queue="q", url="http://peer/b", active=False))   # replace
    got = run(DispatchTargetStore(st).get("q"))
    assert got.url == "http://peer/b" and got.active is False


# ── the happy path: claim → POST → ack, with a correctly signed body ──────────
def test_dispatch_delivers_and_acks_on_2xx():
    poster = _Poster(status=200)
    disp, jobs, _, _ = _wire(poster=poster)
    run(disp.register(queue="push_q", url="http://peer/internal/jobs/run"))
    job, _ = run(jobs.enqueue(queue="push_q", tenant="t1", type="notify", payload={"x": 1}))
    res = run(disp.dispatch_batch())
    assert res == {"enabled": True, "dispatched": 1, "failed": 0}
    assert run(jobs.get(job.id)).status == DONE          # acked
    call = poster.calls[0]
    sent = json.loads(call["body"])["job"]
    assert sent["id"] == job.id and sent["tenant"] == "t1" and sent["payload"] == {"x": 1}
    assert "lease_token" not in sent                     # the fencing token is NEVER sent to the peer


def test_dispatched_body_is_signed_and_verifies():
    poster = _Poster(status=200)
    disp, jobs, _, clock = _wire(poster=poster)
    run(disp.register(queue="push_q", url="http://peer/hook"))
    run(jobs.enqueue(queue="push_q", tenant="t1", type="notify"))
    run(disp.dispatch_batch())
    h, body = poster.calls[0]["headers"], poster.calls[0]["body"]
    sig = h[push_signing.SIGNATURE_HEADER]
    ts = h[push_signing.TIMESTAMP_HEADER]
    # the receiver verifies with the shared secret; a good signature within the window passes
    assert push_signing.verify(SECRET, ts, body, sig, now=clock.now(), max_skew_seconds=300)
    # a wrong secret, a tampered body, and an old timestamp all fail
    assert not push_signing.verify("wrong", ts, body, sig, now=clock.now(), max_skew_seconds=300)
    assert not push_signing.verify(SECRET, ts, body + b"x", sig, now=clock.now(), max_skew_seconds=300)
    assert not push_signing.verify(SECRET, ts, body, sig, now=clock.now() + 10_000, max_skew_seconds=300)


# ── failure → the queue's own retry/backoff/DLQ (no job lost) ─────────────────
def test_a_non_2xx_nacks_into_retry_then_dead_letters():
    poster = _Poster(status=503)                          # peer permanently unhappy
    disp, jobs, _, clock = _wire(poster=poster)
    run(disp.register(queue="push_q", url="http://peer/hook"))
    job, _ = run(jobs.enqueue(queue="push_q", tenant="t1", type="notify"))
    # attempt 1: delivered, 503 → nacked → back to queued with a backoff delay
    assert run(disp.dispatch_batch()) == {"enabled": True, "dispatched": 0, "failed": 1}
    assert run(jobs.get(job.id)).status == QUEUED
    # drive the backoff + retries to exhaustion → DLQ, never dropped
    for _ in range(CFG_JOBS["retry_max_attempts"] + 2):
        clock.advance(4000)
        run(disp.dispatch_batch())
    assert run(jobs.get(job.id)).status == DEAD


def test_a_transport_error_nacks_not_crashes():
    poster = _Poster(boom=True)
    disp, jobs, _, _ = _wire(poster=poster)
    run(disp.register(queue="push_q", url="http://peer/hook"))
    job, _ = run(jobs.enqueue(queue="push_q", tenant="t1", type="notify"))
    assert run(disp.dispatch_batch()) == {"enabled": True, "dispatched": 0, "failed": 1}
    assert run(jobs.get(job.id)).status == QUEUED        # survived, will retry — not lost


# ── tenant isolation: each POST carries ONLY its own tenant ───────────────────
def test_each_push_carries_only_its_own_tenant():
    poster = _Poster(status=200)
    disp, jobs, _, _ = _wire(poster=poster)
    run(disp.register(queue="push_q", url="http://peer/hook"))
    run(jobs.enqueue(queue="push_q", tenant="tenant_a", type="n"))
    run(jobs.enqueue(queue="push_q", tenant="tenant_b", type="n"))
    run(disp.dispatch_batch())
    tenants = sorted(json.loads(c["body"])["job"]["tenant"] for c in poster.calls)
    assert tenants == ["tenant_a", "tenant_b"]
    # no body ever carries two tenants
    for c in poster.calls:
        assert isinstance(json.loads(c["body"])["job"]["tenant"], str)


# ── no double-dispatch across concurrent dispatchers (many pods) ──────────────
def test_two_dispatchers_never_deliver_the_same_job_twice():
    st = MemoryDataStore()
    clock = ManualClock(1000.0)
    jobs = JobService(jobs=JobStore(st), clock=clock, config=CFG_JOBS)
    cfg = {"enabled": True, "batch": 50, "timeout": 5, "allowed_hosts": frozenset(["peer"]),
           "signing_secret": SECRET, "max_skew_seconds": 300}
    p1, p2 = _Poster(200), _Poster(200)
    d1 = Dispatcher(targets=DispatchTargetStore(st), jobs=jobs, clock=clock, config=cfg,
                    poster=p1, headers_for=lambda h: {})
    d2 = Dispatcher(targets=DispatchTargetStore(st), jobs=jobs, clock=clock, config=cfg,
                    poster=p2, headers_for=lambda h: {})
    run(d1.register(queue="push_q", url="http://peer/hook"))
    for _ in range(30):
        run(jobs.enqueue(queue="push_q", tenant="t", type="n"))

    async def both():
        return await asyncio.gather(d1.dispatch_batch(), d2.dispatch_batch())

    run(both())
    delivered = [json.loads(c["body"])["job"]["id"] for c in (p1.calls + p2.calls)]
    assert len(delivered) == len(set(delivered)) == 30    # each job delivered exactly once


# ── disabled / backward compatibility ─────────────────────────────────────────
def test_disabled_is_a_noop():
    poster = _Poster(200)
    disp, jobs, _, _ = _wire(poster=poster, enabled=False)
    run(disp.register(queue="push_q", url="http://peer/hook"))
    run(jobs.enqueue(queue="push_q", tenant="t", type="n"))
    assert run(disp.dispatch_batch()) == {"enabled": False, "dispatched": 0, "failed": 0}
    assert poster.calls == []                             # nothing delivered while disabled


def test_a_queue_with_no_target_is_left_for_pull():
    poster = _Poster(200)
    disp, jobs, _, _ = _wire(poster=poster)
    # a job on an UNREGISTERED queue — push must not touch it; a normal worker still claims it
    job, _ = run(jobs.enqueue(queue="pull_q", tenant="t", type="n"))
    assert run(disp.dispatch_batch()) == {"enabled": True, "dispatched": 0, "failed": 0}
    assert poster.calls == []
    claimed = run(jobs.claim(worker="w", queue="pull_q", batch=1))
    assert claimed and claimed[0]["id"] == job.id        # still deliverable by pull
