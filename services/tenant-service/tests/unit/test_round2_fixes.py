"""Round-2 fixes (tenant-service). Maps to docs/audits/round2/tenant-service.json."""
from __future__ import annotations

import asyncio
import datetime as dt

import pytest

from app.adapters.datastore.memory import MemoryDataStore
from app.adapters.isolation.strategies import SchemaIsolation, SharedRowIsolation
from app.adapters.resolver.resolvers import SubdomainResolver
from app.core.models import DELETED, SUSPENDED
from app.core.tenant_repository import TenantRepository
from app.core.tenant_service import (
    ConcurrentModification, InvalidConfig, TenantService, TenantTerminal,
)


def run(c):
    return asyncio.run(c)


class _Bus:
    driver = "cap"
    async def publish(self, *a): pass
    async def subscribe(self, *a): pass
    async def health(self): return True
    async def close(self): pass


class _Interleave(MemoryDataStore):
    async def get(self, collection, key):
        await asyncio.sleep(0)
        return await super().get(collection, key)


def _svc(store=None, strategies=None):
    store = store if store is not None else MemoryDataStore()
    return TenantService(
        repo=TenantRepository(store), isolation=SharedRowIsolation(),
        resolver=SubdomainResolver("apps.example.com"), event_bus=_Bus(),
        default_isolation="shared-row", default_locale="en", default_timezone="UTC",
        default_calendar="gregorian", slug_min=2, slug_max=48,
        isolation_strategies=strategies)


# r2-01 — concurrent config writes do not lose an update (CAS retry).
def test_concurrent_config_writes_do_not_lose_updates():
    svc = _svc(store=_Interleave())
    t = run(svc.provision("acme", "Acme"))

    async def _go():
        await asyncio.gather(svc.update_config(t, {"a": 1}), svc.update_config(t, {"b": 2}))
    run(_go())
    cfg = run(svc.get(t.id)).config
    assert cfg.get("a") == 1 and cfg.get("b") == 2      # neither write clobbered the other


# r2-02 — a DELETED tombstone cannot be flipped back to suspended/active.
def test_deleted_tenant_cannot_be_resurrected():
    svc = _svc()
    t = run(svc.provision("acme", "Acme"))
    run(svc.delete(t))                                   # tombstone → DELETED
    with pytest.raises(TenantTerminal):
        run(svc.suspend(t))
    with pytest.raises(TenantTerminal):
        run(svc.reactivate(t))
    assert run(svc.get(t.id)).status == DELETED


# r2-03 — the sweep re-reads and skips a tenant reactivated after its schedule.
def test_sweep_skips_a_reactivated_tenant():
    svc = _svc()
    t = run(svc.provision("acme", "Acme"))
    run(svc.close(t, grace_days=30))
    stored = run(svc.get(t.id))
    stored.config["_close"]["grace_until"] = "2000-01-01T00:00:00+00:00"   # grace elapsed
    run(svc._repo.update(stored))
    run(svc.reactivate(t))                               # owner takes it back → ACTIVE, schedule cleared
    now_iso = dt.datetime(2001, 1, 1, tzinfo=dt.timezone.utc).isoformat()
    out = run(svc.close_sweep(now_iso=now_iso))
    assert out["erased"] == 0
    assert run(svc.get(t.id)).status != DELETED


# r2-04 — provision validates isolation_mode; scope() honours the tenant's stored mode.
def test_isolation_mode_is_validated_and_honoured():
    svc = _svc(strategies={"shared-row": SharedRowIsolation(), "schema": SchemaIsolation()})
    t = run(svc.provision("acme", "Acme", isolation_mode="schema"))
    assert svc.scope(t)["mode"] == "schema"             # not silently shared-row
    with pytest.raises(InvalidConfig):
        run(svc.provision("bad", "Bad", isolation_mode="does-not-exist"))


# r2-05 — reserved underscore config keys are refused.
def test_reserved_config_keys_are_refused():
    svc = _svc()
    t = run(svc.provision("acme", "Acme"))
    with pytest.raises(InvalidConfig):
        run(svc.update_config(t, {"_close": {"grace_until": "2000-01-01T00:00:00+00:00"}}))
    with pytest.raises(InvalidConfig):
        run(svc.provision("evil", "Evil", config_overrides={"_close": {}}))


# r2-06 — the sweep continues past a tenant whose delete fails, and reports the failure.
def test_sweep_continues_past_a_failing_delete():
    svc = _svc()
    a = run(svc.provision("aaa", "A"))
    b = run(svc.provision("bbb", "B"))
    for t in (a, b):
        run(svc.close(t, grace_days=30))
        s = run(svc.get(t.id))
        s.config["_close"]["grace_until"] = "2000-01-01T00:00:00+00:00"
        run(svc._repo.update(s))

    real_delete = svc.delete

    async def _flaky(tenant):
        if tenant.id == a.id:
            raise RuntimeError("transient store failure")
        return await real_delete(tenant)
    svc.delete = _flaky

    now_iso = dt.datetime(2001, 1, 1, tzinfo=dt.timezone.utc).isoformat()
    out = run(svc.close_sweep(now_iso=now_iso))
    assert out["erased"] == 1 and out["failed"] == 1    # b erased, a failed — not aborted


# r2-08 — iter_suspended returns oldest-first (sort field matches the model: created_at).
def test_iter_suspended_is_oldest_first():
    store = MemoryDataStore()
    svc = _svc(store=store)
    ids = []
    for i in range(3):
        t = run(svc.provision(f"t{i}", f"T{i}"))
        run(svc.close(t, grace_days=30))
        # stamp distinct created_at so ordering is observable
        row = run(store.get("tenants", t.id))
        row["created_at"] = f"2020-01-0{i+1}T00:00:00+00:00"
        run(store.put("tenants", t.id, row))
        ids.append(t.id)
    rows, _ = run(svc._repo.iter_suspended(limit=10))
    assert [r["id"] for r in rows] == ids               # oldest-first, not arbitrary
