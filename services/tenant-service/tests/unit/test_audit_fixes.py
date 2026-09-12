"""Regression tests for tenant-service audit findings #01 (sweep coverage), #02 (slug TOCTOU), #04 (status guard)."""
from __future__ import annotations

import asyncio
import datetime as dt

import pytest

from app.adapters.datastore.memory import MemoryDataStore
from app.adapters.isolation.strategies import SharedRowIsolation
from app.adapters.resolver.resolvers import SubdomainResolver
from app.core.models import SUSPENDED
from app.core.tenant_repository import SlugTaken, TenantRepository
from app.core.tenant_service import TenantInactive, TenantService


def run(c):
    return asyncio.run(c)


class _Bus:
    driver = "cap"

    async def publish(self, *a):
        pass

    async def subscribe(self, *a):
        pass

    async def health(self):
        return True

    async def close(self):
        pass


def _svc():
    return TenantService(
        repo=TenantRepository(MemoryDataStore()), isolation=SharedRowIsolation(),
        resolver=SubdomainResolver("apps.example.com"), event_bus=_Bus(),
        default_isolation="shared-row", default_locale="en", default_timezone="UTC",
        default_calendar="gregorian", slug_min=2, slug_max=48)


# ── 01: close_sweep reaches EVERY suspended tenant, not just the first page ────
def test_close_sweep_pages_all_suspended_tenants():
    svc = _svc()
    for i in range(5):
        t = run(svc.provision(f"shop{i}", f"Shop {i}"))
        run(svc.close(t, grace_days=1, actor="owner"))     # SUSPENDED, grace_until = now+1day
    later = (dt.datetime.now(dt.timezone.utc) + dt.timedelta(days=2)).isoformat()
    out = run(svc.close_sweep(now_iso=later, page=2))       # page smaller than the suspended set
    assert out["erased"] == 5                                # all closed tenants erased, not just 2


# ── 02: concurrent same-slug provision — exactly one wins ─────────────────────
def test_concurrent_same_slug_provision_wins_once():
    svc = _svc()

    async def _race():
        return await asyncio.gather(svc.provision("dup", "A"), svc.provision("dup", "B"),
                                    return_exceptions=True)

    results = run(_race())
    ok = [r for r in results if not isinstance(r, Exception)]
    taken = [r for r in results if isinstance(r, SlugTaken)]
    assert len(ok) == 1 and len(taken) == 1                  # atomic reserve — one slug, one tenant


# ── 04: a suspended tenant cannot mutate its config/branding ──────────────────
def test_suspended_tenant_cannot_update_config():
    svc = _svc()
    t = run(svc.provision("acme", "Acme"))
    run(svc.close(t, grace_days=30, actor="owner"))          # -> SUSPENDED
    assert t.status == SUSPENDED
    with pytest.raises(TenantInactive):
        run(svc.update_config(t, {"k": "v"}))
