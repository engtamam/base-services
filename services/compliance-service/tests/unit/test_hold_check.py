"""hold_check — the question every retention sweep has to ask and previously could not.

retention.py has always said a hold beats retention and that a held row must be reported as
RETAINED rather than silently skipped. Honouring that from another service was impossible: holds
lived only behind the capability-gated v1 API, so a consuming team had to either give a nightly
cron job an admin token or purge without checking. A team building on v0.6.4 correctly refused the
first and shipped the second, which means data under litigation hold is being deleted on schedule.

The heaviest tests here are about the CACHE, because a stale answer in the unsafe direction is not
a performance bug — it is the destruction of evidence someone is legally required to keep.
"""
from __future__ import annotations

import asyncio

import pytest

from app.adapters.datastore.memory import MemoryDataStore
from app.core.compliance_service import ComplianceService
from app.core.stores import HoldStore


def run(c):
    return asyncio.run(c)


class _Clock:
    def __init__(self):
        self.t = 1_700_000_000.0

    def now(self):
        return self.t


class _Cache:
    """A real enough cache: stores strings, and can be told to fail."""

    def __init__(self):
        self.data: dict[str, str] = {}
        self.broken = False

    async def get(self, k):
        if self.broken:
            raise RuntimeError("cache down")
        return self.data.get(k)

    async def set(self, k, v, ttl=None):
        if self.broken:
            raise RuntimeError("cache down")
        self.data[k] = v


def _svc(cache=None):
    store = MemoryDataStore()
    svc = ComplianceService.__new__(ComplianceService)
    svc._cache = cache
    svc._holds = HoldStore(store)
    svc._clock = _Clock()
    svc._cfg = {"list_limit": 100, "hold_cache_ttl": 30}
    svc._emitted = []

    async def _emit(name, payload):
        svc._emitted.append((name, payload))

    svc._emit = _emit
    return svc


def test_an_unheld_subject_is_not_held():
    svc = _svc()
    out = run(svc.hold_check(tenant="acme", subject="ada@example.com"))
    assert out["held"] is False
    assert out["hold_ids"] == []


def test_a_held_subject_reports_the_hold_IDS_not_just_true():
    """A caller that must report WHY it retained a row needs something to name. A bare `true`
    sends it back to the admin API for the answer, which is the access problem all over again."""
    svc = _svc()
    h = run(svc.place_hold(tenant="acme", subject="ada@example.com", reason="litigation"))
    out = run(svc.hold_check(tenant="acme", subject="ada@example.com"))
    assert out["held"] is True
    assert out["hold_ids"] == [h.id]


def test_holds_do_not_leak_across_tenants():
    svc = _svc()
    run(svc.place_hold(tenant="acme", subject="ada@example.com", reason="litigation"))
    assert run(svc.hold_check(tenant="globex", subject="ada@example.com"))["held"] is False


def test_a_released_hold_stops_blocking():
    svc = _svc()
    h = run(svc.place_hold(tenant="acme", subject="ada@example.com", reason="litigation"))
    run(svc.release_hold(h.id))
    assert run(svc.hold_check(tenant="acme", subject="ada@example.com"))["held"] is False


# ── the cache, which is the dangerous part ─────────────────────────────────────
def test_a_hold_placed_AFTER_a_cached_miss_is_seen_immediately():
    """THE test. Without the generation bump, a sweeper that asked a moment ago keeps being told
    the subject is free for the whole TTL — and purges data that is now under hold."""
    cache = _Cache()
    svc = _svc(cache)

    assert run(svc.hold_check(tenant="acme", subject="ada@example.com"))["held"] is False
    run(svc.place_hold(tenant="acme", subject="ada@example.com", reason="litigation"))

    out = run(svc.hold_check(tenant="acme", subject="ada@example.com"))
    assert out["held"] is True, "a cached 'not held' outlived the hold that contradicts it"
    assert out["cached"] is False


def test_a_repeated_check_is_served_from_cache():
    """It is a hot path — asked before every purge in every service. If it never cached, the
    honest answer would be that this endpoint is too expensive to call, and callers would stop."""
    cache = _Cache()
    svc = _svc(cache)
    run(svc.hold_check(tenant="acme", subject="ada@example.com"))
    assert run(svc.hold_check(tenant="acme", subject="ada@example.com"))["cached"] is True


def test_a_release_is_also_seen_immediately():
    cache = _Cache()
    svc = _svc(cache)
    h = run(svc.place_hold(tenant="acme", subject="ada@example.com", reason="litigation"))
    assert run(svc.hold_check(tenant="acme", subject="ada@example.com"))["held"] is True
    run(svc.release_hold(h.id))
    assert run(svc.hold_check(tenant="acme", subject="ada@example.com"))["held"] is False


def test_a_broken_cache_still_answers_from_the_store():
    """The answer is COMPUTED, never assumed. There must be no path that reports 'not held'
    without having looked — an outage in the cache must not become permission to delete."""
    cache = _Cache()
    svc = _svc(cache)
    run(svc.place_hold(tenant="acme", subject="ada@example.com", reason="litigation"))
    cache.broken = True

    out = run(svc.hold_check(tenant="acme", subject="ada@example.com"))
    assert out["held"] is True


def test_it_works_with_no_cache_configured_at_all():
    """Zero-infra default. The control must not depend on Redis being deployed."""
    svc = _svc(cache=None)
    run(svc.place_hold(tenant="acme", subject="ada@example.com", reason="litigation"))
    assert run(svc.hold_check(tenant="acme", subject="ada@example.com"))["held"] is True
