"""Non-functional guards — these two tests would have caught the Phase-16 defects.

D2: flag evaluation is the highest-QPS path in the platform. It must not write.
D1: a config knob with no consumer is a lie in the API surface.
"""
from __future__ import annotations

import asyncio
import pathlib
import re

from app.adapters.cache.memory import MemoryCache
from app.adapters.datastore.memory import MemoryDataStore
from app.adapters.entitlement.null import NullEntitlement
from app.adapters.flags.local import LocalFlags
from app.core.flag_service import FlagService
from app.core.stores import FlagStore, SegmentStore


def run(c):
    return asyncio.run(c)


class CountingStore(MemoryDataStore):
    def __init__(self):
        super().__init__()
        self.writes = 0
        self.reads = 0

    async def put(self, c, k, v):
        self.writes += 1
        return await super().put(c, k, v)

    async def get(self, c, k):
        self.reads += 1
        return await super().get(c, k)

    async def scan_all(self, c, limit=100):
        self.reads += 1
        return await super().scan_all(c, limit)


def _svc(st, cache):
    return FlagService(store=FlagStore(st), provider=LocalFlags(st), entitlement=NullEntitlement(),
                       segments=SegmentStore(st), cache=cache, salt="s",
                       cache_ttl=300, last_seen_threshold=3600)


# ── D2: no writes on the hot path ─────────────────────────────────────────────
def test_evaluate_all_does_zero_writes_in_steady_state():
    st, cache = CountingStore(), MemoryCache()
    b = _svc(st, cache)
    for i in range(10):
        run(b.upsert(key=f"f{i}", enabled=True, rollout_pct=100))
    run(b.evaluate_all({"user": "alice"}))          # warm: stamps last_evaluated once per flag
    st.writes = 0
    for _ in range(50):
        run(b.evaluate_all({"user": "alice"}))
    assert st.writes == 0, f"hot path wrote {st.writes} times — write amplification regression"


def test_evaluate_single_does_zero_writes_in_steady_state():
    st, cache = CountingStore(), MemoryCache()
    b = _svc(st, cache)
    run(b.upsert(key="f", enabled=True, rollout_pct=100))
    run(b.evaluate("f", {"user": "a"}))
    st.writes = 0
    for _ in range(50):
        run(b.evaluate("f", {"user": "a"}))
    assert st.writes == 0


def test_last_evaluated_is_written_once_past_the_threshold():
    from app.core.models import Flag

    st = CountingStore()
    fs = FlagStore(st)
    run(fs.save(Flag(key="f")))
    st.writes = 0
    # fresh stamp (last_evaluated=0) → writes
    assert run(fs.maybe_touch("f", 10_000.0, last_evaluated=0.0, threshold_seconds=3600)) is True
    # within the threshold → no write
    assert run(fs.maybe_touch("f", 10_100.0, last_evaluated=10_000.0, threshold_seconds=3600)) is False
    # past the threshold → writes again
    assert run(fs.maybe_touch("f", 20_000.0, last_evaluated=10_000.0, threshold_seconds=3600)) is True


# ── D1: the cache is actually wired ───────────────────────────────────────────
def test_definitions_are_cached_between_requests():
    st, cache = CountingStore(), MemoryCache()
    b = _svc(st, cache)
    run(b.upsert(key="f", enabled=True, rollout_pct=100))
    run(b.evaluate_all({"user": "a"}))       # warm the definition cache + last-seen marker
    st.reads = 0
    for _ in range(20):
        run(b.evaluate_all({"user": "a"}))
    assert st.reads == 0, f"cache not used — {st.reads} datastore reads on the hot path"


def test_cache_is_invalidated_on_write():
    st, cache = CountingStore(), MemoryCache()
    b = _svc(st, cache)
    run(b.upsert(key="f", enabled=True, rollout_pct=100))
    assert run(b.evaluate("f", {"user": "a"}))["enabled"] is True
    run(b.upsert(key="f", enabled=False))                 # must bust the cache
    assert run(b.evaluate("f", {"user": "a"}))["reason"] == "kill_switch"


def test_cache_outage_never_breaks_evaluation():
    class BrokenCache:
        driver = "broken"

        async def get(self, key):
            raise RuntimeError("cache down")

        async def set(self, key, value, ttl_seconds=None):
            raise RuntimeError("cache down")

        async def delete(self, key):
            raise RuntimeError("cache down")

    st = MemoryDataStore()
    b = _svc(st, BrokenCache())
    run(b.upsert(key="f", enabled=True, rollout_pct=100))
    assert run(b.evaluate("f", {"user": "a"}))["enabled"] is True


# ── D1 (root cause): every config knob must have a consumer ────────────────────
def test_every_settings_field_is_consumed_somewhere():
    """An unwired knob (like FLAG_CACHE_TTL_SECONDS was) is a lie in the API surface.

    'Consumed' = referenced anywhere in the service other than its own declaration line —
    including a `@property` in config.py (EGRESS_ALLOWED_HOSTS) and the run command
    (HOST/PORT in Dockerfile/Makefile).
    """
    from app.config import Settings

    root = pathlib.Path(__file__).resolve().parents[2]
    files = [p for p in root.rglob("*") if p.is_file()
             and p.suffix in (".py", ".toml", "") and "tests" not in p.parts
             and ".venv" not in p.parts and "__pycache__" not in p.parts]
    lines: list[str] = []
    for p in files:
        try:
            lines.extend(p.read_text(encoding="utf-8").splitlines())
        except (UnicodeDecodeError, OSError):
            continue

    unused = []
    for name in Settings.model_fields:
        decl = re.compile(rf"^\s*{re.escape(name)}\s*:")      # its own declaration line
        uses = [ln for ln in lines if re.search(rf"\b{re.escape(name)}\b", ln) and not decl.match(ln)]
        if not uses:
            unused.append(name)
    assert not unused, f"config knobs declared but never consumed: {unused}"
