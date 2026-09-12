"""Round-2 fixes (feature-flag-service): entitlement fail-closed, per-request memoization, erasure
cache-invalidation + CAS. Maps to docs/audits/round2/feature-flag-service.json r2-05, r2-06, r2-09.
"""
from __future__ import annotations

import asyncio

from app.adapters.cache.memory import MemoryCache
from app.adapters.datastore.memory import MemoryDataStore
from app.adapters.entitlement.null import NullEntitlement
from app.adapters.flags.local import LocalFlags
from app.core.erasure import FeatureFlagErasure
from app.core.flag_service import FlagService
from app.core.stores import FlagStore, SegmentStore


def run(c):
    return asyncio.run(c)


def _svc(st, entitlement=None, cache=None, **kw):
    return FlagService(store=FlagStore(st), provider=LocalFlags(st),
                       entitlement=entitlement or NullEntitlement(),
                       segments=SegmentStore(st), cache=cache or MemoryCache(), salt="s", **kw)


# r2-05 — a flag that requires an entitlement is DENIED under the null (no-op) driver, not opened.
def test_requires_entitlement_fails_closed_under_null_driver():
    st = MemoryDataStore()
    b = _svc(st)
    run(b.upsert(key="premium", enabled=True, rollout_pct=100, requires_entitlement="premium"))
    r = run(b.evaluate("premium", {"tenant": "acme", "user": "u1"}))
    assert r["enabled"] is False           # was silently True (fail-open) under NullEntitlement


class _CountingEnt:
    driver = "counting"

    def __init__(self):
        self.calls = 0

    def allowed(self, *, tenant, key):
        self.calls += 1
        return True


# r2-06 — evaluate_all checks each (tenant,key) entitlement at most once (memoized per request).
def test_evaluate_all_memoizes_entitlement_checks():
    st = MemoryDataStore()
    ent = _CountingEnt()
    b = _svc(st, entitlement=ent)
    for i in range(5):
        run(b.upsert(key=f"f{i}", enabled=True, rollout_pct=100, requires_entitlement="premium"))
    ent.calls = 0
    out = run(b.evaluate_all({"tenant": "acme", "user": "u1"}))
    assert len(out) == 5
    assert ent.calls == 1                  # one check for the single (acme, premium) pair, not 5


# r2-09 — erasure removes the override via CAS and invalidates the cache (no stale eval).
def test_erasure_invalidates_cache_and_removes_override():
    st = MemoryDataStore()
    cache = MemoryCache()
    b = _svc(st, cache=cache)
    run(b.upsert(key="beta", enabled=True, rollout_pct=0))   # on-switch on, but 0% rollout → off
    run(b.set_override("beta", scope="user", subject="victim", value=True))
    # prime the cache: evaluate so flag:beta is cached with the override
    assert run(b.evaluate("beta", {"tenant": "acme", "user": "victim"}))["enabled"] is True

    eraser = FeatureFlagErasure(store=st, pii_salt="s", cache=cache)
    res = run(eraser.erase("acme", "victim"))
    assert res["erased"] == ["user_overrides:1"]

    # after erasure the override no longer influences evaluation (cache was invalidated)
    assert run(b.evaluate("beta", {"tenant": "acme", "user": "victim"}))["enabled"] is False
    # idempotent second erase
    assert run(eraser.erase("acme", "victim"))["already_absent"] is True
