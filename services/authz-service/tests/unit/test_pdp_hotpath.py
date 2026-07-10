"""Non-functional guards (per memory `phase-doc-rules`).

`check()` is the platform PDP — it runs on effectively every request. It must not scan the
policy table or fan out role lookups on every call.
"""
from __future__ import annotations

import asyncio
import pathlib
import re

from app.adapters.cache.memory import MemoryCache
from app.adapters.datastore.memory import MemoryDataStore
from app.core.authz_service import AuthzService
from app.core.stores import AssignmentStore, PolicyStore, RoleStore


def run(c):
    return asyncio.run(c)


class CountingStore(MemoryDataStore):
    def __init__(self):
        super().__init__()
        self.reads = self.writes = self.scans = 0

    async def get(self, c, k):
        self.reads += 1
        return await super().get(c, k)

    async def put(self, c, k, v):
        self.writes += 1
        return await super().put(c, k, v)

    async def list(self, c, limit=100):
        self.scans += 1
        return await super().list(c, limit)


def _svc(st, cache):
    class Engine:
        driver = "e"

        def decide(self, ctx):
            from app.ports.policy_engine import Decision
            allow = "orders.read" in ctx["effective_caps"]
            return Decision(allow=allow, reason="cap" if allow else "deny", matched_policy=None)

    return AuthzService(roles=RoleStore(st), policies=PolicyStore(st), assignments=AssignmentStore(st),
                        engine=Engine(), cache=cache, cache_ttl=300)


def _seed(w, st):
    run(w.create_role("t1", "reader", ["orders.read"]))
    run(w.set_subject_roles("alice", "t1", roles=[r["id"] for r in run(RoleStore(st).list("t1"))]))
    for i in range(20):
        run(w.create_policy("t1", "allow", f"a{i}", "*"))


def test_check_does_not_scan_or_read_after_warm():
    st, cache = CountingStore(), MemoryCache()
    w = _svc(st, cache)
    _seed(w, st)
    run(w.check(subject="alice", action="orders.read", resource="o1", tenant="t1"))   # warm
    st.scans = st.reads = 0
    for _ in range(50):
        run(w.check(subject="alice", action="orders.read", resource="o1", tenant="t1"))
    assert st.scans == 0, f"PDP scanned the policy table {st.scans} times"
    assert st.reads == 0, f"PDP did {st.reads} datastore reads on the hot path"


def test_policy_change_is_reflected_immediately():
    st, cache = CountingStore(), MemoryCache()
    w = _svc(st, cache)
    _seed(w, st)
    assert run(w.check(subject="alice", action="orders.read", resource="o", tenant="t1")).allow is True
    # revoke the role's cap → next decision must see it (generation bump busts the cache)
    role_id = run(RoleStore(st).list("t1"))[0]["id"]
    role = run(RoleStore(st).get(role_id))
    run(w.update_role(role, capabilities=[]))
    assert run(w.check(subject="alice", action="orders.read", resource="o", tenant="t1")).allow is False


def test_assignment_change_invalidates_subject_caps():
    st, cache = CountingStore(), MemoryCache()
    w = _svc(st, cache)
    _seed(w, st)
    run(w.check(subject="alice", action="orders.read", resource="o", tenant="t1"))
    run(w.set_subject_roles("alice", "t1", roles=[]))          # strip roles
    assert run(w.check(subject="alice", action="orders.read", resource="o", tenant="t1")).allow is False


def test_cache_outage_never_breaks_a_decision():
    class BrokenCache:
        driver = "broken"

        async def get(self, key):
            raise RuntimeError("down")

        async def set(self, key, value, ttl_seconds=None):
            raise RuntimeError("down")

    st = MemoryDataStore()
    w = _svc(st, BrokenCache())
    run(w.create_role("t1", "reader", ["orders.read"]))
    run(w.set_subject_roles("alice", "t1", roles=[r["id"] for r in run(RoleStore(st).list("t1"))]))
    assert run(w.check(subject="alice", action="orders.read", resource="o", tenant="t1")).allow is True


def test_tenant_isolation_in_cache():
    st, cache = CountingStore(), MemoryCache()
    w = _svc(st, cache)
    run(w.create_role("t1", "reader", ["orders.read"]))
    run(w.set_subject_roles("alice", "t1", roles=[r["id"] for r in run(RoleStore(st).list("t1"))]))
    run(w.check(subject="alice", action="orders.read", resource="o", tenant="t1"))
    # same subject id in another tenant must NOT inherit t1's cached caps
    assert run(w.check(subject="alice", action="orders.read", resource="o", tenant="t2")).allow is False


def test_every_settings_field_is_consumed_somewhere():
    from app.config import Settings

    root = pathlib.Path(__file__).resolve().parents[2]
    files = [p for p in root.rglob("*") if p.is_file() and p.suffix in (".py", ".toml", "")
             and "tests" not in p.parts and ".venv" not in p.parts and "__pycache__" not in p.parts]
    lines: list[str] = []
    for p in files:
        try:
            lines.extend(p.read_text(encoding="utf-8").splitlines())
        except (UnicodeDecodeError, OSError):
            continue
    unused = []
    for name in Settings.model_fields:
        decl = re.compile(rf"^\s*{re.escape(name)}\s*:")
        if not [ln for ln in lines if re.search(rf"\b{re.escape(name)}\b", ln) and not decl.match(ln)]:
            unused.append(name)
    assert not unused, f"config knobs declared but never consumed: {unused}"
