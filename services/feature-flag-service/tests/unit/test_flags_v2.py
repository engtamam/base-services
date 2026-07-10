"""v2: bucket_by, weighted variants, segments, prerequisites, scheduling, exposures, audit, bypass."""
from __future__ import annotations

import asyncio
import time
from collections import Counter

import pytest

from app.adapters.cache.memory import MemoryCache
from app.adapters.datastore.memory import MemoryDataStore
from app.adapters.entitlement.null import NullEntitlement
from app.adapters.flags.local import LocalFlags
from app.core.config_service import MaintenanceService
from app.core.flag_service import FlagService
from app.core.models import Segment
from app.core.rollout import pick_variant, subject_for, validate_variants
from app.core.stores import FlagStore, MaintenanceStore, SegmentStore


def run(c):
    return asyncio.run(c)


class Bus:
    def __init__(self):
        self.events = []

    async def publish(self, event, payload):
        self.events.append((event, payload))

    def of(self, name):
        return [p for e, p in self.events if e == name]


def _svc(st, bus=None, segs=None, **kw):
    return FlagService(store=FlagStore(st), provider=LocalFlags(st), entitlement=NullEntitlement(),
                       segments=segs or SegmentStore(st), cache=MemoryCache(), event_bus=bus,
                       salt="s", **kw)


# ── C1: bucket_by ─────────────────────────────────────────────────────────────
def test_subject_for_selects_the_hashed_identity():
    ctx = {"tenant": "acme", "user": "u1", "attrs": {"device": "d9"}}
    assert subject_for("user", ctx) == "u1"
    assert subject_for("tenant", ctx) == "acme"
    assert subject_for("attr:device", ctx) == "d9"
    assert subject_for("attr:missing", ctx) == ""            # → not in rollout (fail-safe)


def test_bucket_by_tenant_keeps_a_whole_company_consistent():
    st = MemoryDataStore()
    b = _svc(st)
    run(b.upsert(key="schema_v2", enabled=True, rollout_pct=50, bucket_by="tenant"))
    for tenant in ("acme", "globex", "initech"):
        vals = [run(b.evaluate("schema_v2", {"tenant": tenant, "user": f"u{i}"}))["enabled"]
                for i in range(6)]
        assert len(set(vals)) == 1, f"{tenant} split mid-company — a schema rollout footgun"


def test_bucket_by_user_varies_within_a_tenant():
    st = MemoryDataStore()
    b = _svc(st)
    run(b.upsert(key="ui_test", enabled=True, rollout_pct=50, bucket_by="user"))
    vals = [run(b.evaluate("ui_test", {"tenant": "acme", "user": f"u{i}"}))["enabled"] for i in range(30)]
    assert len(set(vals)) > 1


def test_missing_bucket_subject_is_not_in_rollout():
    st = MemoryDataStore()
    b = _svc(st)
    run(b.upsert(key="f", enabled=True, rollout_pct=50, bucket_by="tenant"))
    assert run(b.evaluate("f", {"user": "u1"}))["reason"] == "default"      # no tenant → fail-safe


# ── E1: weighted variants ─────────────────────────────────────────────────────
def test_variant_weights_validated():
    validate_variants({"a": 50, "b": 50})
    validate_variants({})
    with pytest.raises(ValueError):
        validate_variants({"a": 60, "b": 60})
    with pytest.raises(ValueError):
        validate_variants({"a": -10, "b": 110})


def test_variant_assignment_is_sticky_and_weighted():
    assert pick_variant("s", "f", "u1", {"a": 50, "b": 50}) == pick_variant("s", "f", "u1", {"a": 50, "b": 50})
    c = Counter(pick_variant("s", "exp", f"u{i}", {"control": 50, "treatment": 50}) for i in range(3000))
    assert 0.45 < c["control"] / 3000 < 0.55
    c3 = Counter(pick_variant("s", "e3", f"u{i}", {"a": 70, "b": 20, "c": 10}) for i in range(4000))
    assert 0.66 < c3["a"] / 4000 < 0.74 and 0.16 < c3["b"] / 4000 < 0.24 and 0.07 < c3["c"] / 4000 < 0.13


def test_variant_rollout_reason_and_no_subject():
    st = MemoryDataStore()
    b = _svc(st)
    run(b.upsert(key="exp", enabled=True, rollout_pct=100, variants={"control": 50, "treatment": 50}))
    r = run(b.evaluate("exp", {"user": "u1"}))
    assert r["reason"] == "variant_rollout" and r["variant"] in ("control", "treatment")
    assert pick_variant("s", "f", "", {"a": 100}) is None


def test_bad_variant_weights_rejected_at_upsert():
    b = _svc(MemoryDataStore())
    with pytest.raises(ValueError):
        run(b.upsert(key="bad", enabled=True, variants={"a": 30, "b": 30}))


# ── E3: segments ──────────────────────────────────────────────────────────────
def test_segment_match_and_non_member():
    st = MemoryDataStore()
    segs = SegmentStore(st)
    run(segs.save(Segment(key="ksa_pro", rules=[{"conditions": [
        {"attribute": "country", "op": "equals", "value": "SA"},
        {"attribute": "plan", "op": "equals", "value": "pro"}]}])))
    b = _svc(st, segs=segs)
    run(b.upsert(key="beta", enabled=True, rollout_pct=0, segments=["ksa_pro"]))
    assert run(b.evaluate("beta", {"user": "x", "attrs": {"country": "SA", "plan": "pro"}}))["reason"] == "segment_match"
    assert run(b.evaluate("beta", {"user": "x", "attrs": {"country": "EG", "plan": "pro"}}))["reason"] == "default"


def test_unknown_segment_never_matches():
    st = MemoryDataStore()
    b = _svc(st)
    run(b.upsert(key="f", enabled=True, rollout_pct=0, segments=["does_not_exist"]))
    assert run(b.evaluate("f", {"user": "x", "attrs": {}}))["reason"] == "default"


# ── E4: prerequisites ─────────────────────────────────────────────────────────
def test_prerequisite_gates_the_child():
    st = MemoryDataStore()
    b = _svc(st)
    run(b.upsert(key="parent", enabled=True, rollout_pct=100))
    run(b.upsert(key="child", enabled=True, rollout_pct=100, prerequisites=[{"key": "parent"}]))
    assert run(b.evaluate("child", {"user": "a"}))["enabled"] is True
    run(b.upsert(key="parent", enabled=False))
    assert run(b.evaluate("child", {"user": "a"}))["reason"] == "prerequisite_failed"


def test_prerequisite_variant_must_match():
    st = MemoryDataStore()
    b = _svc(st)
    run(b.upsert(key="exp", enabled=True, rollout_pct=100, variants={"a": 100}))
    run(b.upsert(key="dep", enabled=True, rollout_pct=100, prerequisites=[{"key": "exp", "variant": "a"}]))
    assert run(b.evaluate("dep", {"user": "u"}))["enabled"] is True
    run(b.upsert(key="dep2", enabled=True, rollout_pct=100, prerequisites=[{"key": "exp", "variant": "b"}]))
    assert run(b.evaluate("dep2", {"user": "u"}))["reason"] == "prerequisite_failed"


def test_prerequisite_cycle_and_missing_fail_closed():
    st = MemoryDataStore()
    b = _svc(st)
    run(b.upsert(key="cyc", enabled=True, rollout_pct=100, prerequisites=[{"key": "cyc"}]))
    assert run(b.evaluate("cyc", {"user": "a"}))["reason"] == "prerequisite_failed"
    run(b.upsert(key="orphan", enabled=True, rollout_pct=100, prerequisites=[{"key": "ghost"}]))
    assert run(b.evaluate("orphan", {"user": "a"}))["reason"] == "prerequisite_failed"


def test_prerequisite_depth_cap():
    st = MemoryDataStore()
    b = _svc(st, prereq_max_depth=2)
    run(b.upsert(key="a", enabled=True, rollout_pct=100))
    run(b.upsert(key="b", enabled=True, rollout_pct=100, prerequisites=[{"key": "a"}]))
    run(b.upsert(key="c", enabled=True, rollout_pct=100, prerequisites=[{"key": "b"}]))
    run(b.upsert(key="d", enabled=True, rollout_pct=100, prerequisites=[{"key": "c"}]))
    assert run(b.evaluate("d", {"user": "a"}))["reason"] == "prerequisite_failed"   # depth cap


# ── E5: scheduled flags ───────────────────────────────────────────────────────
def test_scheduled_window():
    st = MemoryDataStore()
    b = _svc(st)
    now = time.time()
    run(b.upsert(key="launch", enabled=True, rollout_pct=100, start=now + 60))
    assert run(b.evaluate("launch", {"user": "a"}))["reason"] == "scheduled"       # not started
    run(b.upsert(key="launch", start=now - 60, end=now + 60))
    assert run(b.evaluate("launch", {"user": "a"}))["reason"] == "rollout"          # inside
    run(b.upsert(key="launch", start=now - 120, end=now - 60))
    assert run(b.evaluate("launch", {"user": "a"}))["reason"] == "scheduled"        # expired


# ── E2: exposure events ───────────────────────────────────────────────────────
def test_exposure_event_emitted_with_variant_and_bucket():
    st, bus = MemoryDataStore(), Bus()
    b = _svc(st, bus=bus)
    run(b.upsert(key="exp", enabled=True, rollout_pct=100, variants={"a": 100}, bucket_by="tenant"))
    bus.events.clear()
    run(b.evaluate("exp", {"tenant": "acme", "user": "u1"}))
    exp = bus.of("flag.exposed")
    assert len(exp) == 1
    assert exp[0]["key"] == "exp" and exp[0]["variant"] == "a"
    assert exp[0]["bucket_by"] == "tenant" and exp[0]["subject"] == "acme"


def test_exposure_sampling_zero_emits_nothing():
    st, bus = MemoryDataStore(), Bus()
    b = _svc(st, bus=bus, exposure_sample_pct=0)
    run(b.upsert(key="f", enabled=True, rollout_pct=100))
    bus.events.clear()
    run(b.evaluate("f", {"user": "u"}))
    assert bus.of("flag.exposed") == []


def test_exposure_never_blocks_evaluation():
    class BadBus:
        async def publish(self, *a, **k):
            raise RuntimeError("bus down")

    st = MemoryDataStore()
    b = _svc(st, bus=BadBus())
    run(b.upsert(key="f", enabled=True, rollout_pct=100))
    assert run(b.evaluate("f", {"user": "u"}))["enabled"] is True


# ── E6: change audit ──────────────────────────────────────────────────────────
def test_change_event_carries_actor_and_diff():
    st, bus = MemoryDataStore(), Bus()
    b = _svc(st, bus=bus)
    run(b.upsert(key="f", enabled=False, actor="alice"))
    bus.events.clear()
    run(b.upsert(key="f", enabled=True, actor="bob"))
    ev = bus.of("flag.changed")[0]
    assert ev["actor"] == "bob"
    assert ev["before"]["enabled"] is False and ev["after"]["enabled"] is True


def test_override_and_archive_are_audited():
    st, bus = MemoryDataStore(), Bus()
    b = _svc(st, bus=bus)
    run(b.upsert(key="f", enabled=True))
    bus.events.clear()
    run(b.set_override("f", scope="user", subject="x", value=True, actor="carol"))
    assert bus.of("flag.changed")[0]["actor"] == "carol"
    run(b.archive("f", actor="dave"))
    assert bus.of("flag.archived")[0]["actor"] == "dave"


# ── E7: maintenance bypass ────────────────────────────────────────────────────
def test_maintenance_bypass_allowlist():
    st = MemoryDataStore()
    ms = MaintenanceService(store=MaintenanceStore(st), bypass_subjects=frozenset({"oncall", "healthcheck"}))
    run(ms.set(module="global", enabled=True, message="upgrade"))
    assert run(ms.status())["active"] is True                         # normal user: down
    s = run(ms.status(subject="oncall"))
    assert s["active"] is False and s["bypassed"] is True             # admin can get in to fix it
    assert run(ms.status(subject="random"))["active"] is True
