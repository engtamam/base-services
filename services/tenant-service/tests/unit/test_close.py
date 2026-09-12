"""Owner self-close — 52.3. Suspend now, erase after grace, recoverable until then."""
from __future__ import annotations

import asyncio
import datetime as dt

from app.adapters.datastore.memory import MemoryDataStore
from app.adapters.isolation.strategies import SharedRowIsolation
from app.adapters.resolver.resolvers import SubdomainResolver
from app.core.models import DELETED, SUSPENDED
from app.core.tenant_repository import TenantRepository
from app.core.tenant_service import TenantService


def run(c):
    return asyncio.run(c)


class CapBus:
    driver = "cap"

    def __init__(self):
        self.events = []

    async def publish(self, topic, event):
        self.events.append(topic)

    async def subscribe(self, *a):
        pass

    async def health(self):
        return True

    async def close(self):
        pass


def _svc(bus=None):
    store = MemoryDataStore()
    return TenantService(
        repo=TenantRepository(store), isolation=SharedRowIsolation(),
        resolver=SubdomainResolver("apps.example.com"), event_bus=bus or CapBus(),
        default_isolation="shared-row", default_locale="en", default_timezone="UTC",
        default_calendar="gregorian", slug_min=2, slug_max=48)


def test_close_suspends_and_sets_a_grace_deadline():
    bus = CapBus()
    svc = _svc(bus)
    t = run(svc.provision("acme", "Acme"))
    res = run(svc.close(t, grace_days=30, actor="owner@acme"))
    assert res["closed"] and res["status"] == SUSPENDED and res["grace_until"]
    assert "tenant.close_scheduled" in bus.events
    # writes are lose-safe (reload → mutate → CAS), so re-read the authoritative row
    assert run(svc.get(t.id)).config["_close"]["closed_by"] == "owner@acme"


def test_close_is_idempotent():
    svc = _svc()
    t = run(svc.provision("acme", "Acme"))
    a = run(svc.close(t, grace_days=30))
    b = run(svc.close(t, grace_days=30))
    assert b.get("idempotent") is True and b["grace_until"] == a["grace_until"]


def test_zero_grace_erases_immediately():
    svc = _svc()
    t = run(svc.provision("acme", "Acme"))
    res = run(svc.close(t, grace_days=0))
    assert res["status"] == DELETED and res["closed"]


def test_reactivating_within_grace_takes_the_close_back():
    svc = _svc()
    t = run(svc.provision("acme", "Acme"))
    run(svc.close(t, grace_days=30))
    run(svc.reactivate(t))
    assert "_close" not in run(svc.get(t.id)).config   # schedule cleared → sweep will not erase it


def test_sweep_erases_only_tenants_past_their_grace():
    svc = _svc()
    past = run(svc.provision("old", "Old"))
    run(svc.close(past, grace_days=30))
    # force its grace into the past (re-read the authoritative row, then persist)
    past = run(svc.get(past.id))
    past.config["_close"]["grace_until"] = "2000-01-01T00:00:00+00:00"
    run(svc._repo.update(past))
    live = run(svc.provision("new", "New"))
    run(svc.close(live, grace_days=30))          # grace still in the future
    now_iso = dt.datetime(2001, 1, 1, tzinfo=dt.timezone.utc).isoformat()
    out = run(svc.close_sweep(now_iso=now_iso))
    assert out["erased"] == 1
    assert run(svc.get(past.id)).status == DELETED       # grace elapsed → erased (tombstone)
    assert run(svc.get(live.id)).status == SUSPENDED     # still within grace, untouched
