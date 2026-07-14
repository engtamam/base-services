"""Unit tests: repository, provisioning, resolution, lifecycle+events, isolation."""
from __future__ import annotations

import asyncio

import pytest

from app.adapters.datastore.memory import MemoryDataStore
from app.adapters.isolation.strategies import SchemaIsolation, SharedRowIsolation
from app.adapters.resolver.resolvers import SubdomainResolver
from app.core.tenant_repository import DomainTaken, SlugTaken, TenantRepository
from app.core.tenant_service import InvalidSlug, TenantService


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


def _svc(isolation=None, bus=None):
    store = MemoryDataStore()
    return TenantService(
        repo=TenantRepository(store), isolation=isolation or SharedRowIsolation(),
        resolver=SubdomainResolver("apps.example.com"), event_bus=bus or CapBus(),
        default_isolation="shared-row", default_locale="en", default_timezone="UTC",
        default_calendar="gregorian", slug_min=2, slug_max=48,
    )


def test_provision_and_get():
    svc = _svc()
    t = run(svc.provision("acme", "Acme Inc", domains=["acme.com"]))
    assert t.slug == "acme" and t.status == "active"
    assert t.config["locale"] == "en" and t.config["timezone"] == "UTC"
    assert run(svc.get(t.id)).name == "Acme Inc"


def test_invalid_slug_rejected():
    with pytest.raises(InvalidSlug):
        run(_svc().provision("A_C", "x"))   # uppercase/underscore invalid
    with pytest.raises(InvalidSlug):
        run(_svc().provision("a", "x"))      # too short (< 2)


def test_slug_and_domain_uniqueness():
    svc = _svc()
    run(svc.provision("acme", "Acme", domains=["acme.com"]))
    with pytest.raises(SlugTaken):
        run(svc.provision("acme", "Other"))
    with pytest.raises(DomainTaken):
        run(svc.provision("acme2", "Other", domains=["acme.com"]))


def test_resolve_by_host_subdomain():
    svc = _svc()
    run(svc.provision("acme", "Acme"))
    t = run(svc.resolve_by_host("acme.apps.example.com"))
    assert t is not None and t.slug == "acme"
    assert run(svc.resolve_by_host("nope.other.com")) is None


def test_lifecycle_emits_events():
    bus = CapBus()
    svc = _svc(bus=bus)
    t = run(svc.provision("acme", "Acme"))
    run(svc.suspend(t))
    run(svc.reactivate(t))
    run(svc.delete(t))
    assert bus.events == ["tenant.created", "tenant.suspended", "tenant.reactivated", "tenant.deleted"]
    # NOT purged: a TOMBSTONE. `tenant.deleted` is published at most once, so a peer that missed
    # it must still be able to learn the truth by READING. A read is self-healing; a missed event
    # is wrong for ever. And the marker is not a copy of the tenant — the name and slug are gone.
    stone = run(svc.get(t.id))
    assert stone is not None and stone.status == "deleted" and stone.deleted_at
    assert stone.name == "" and stone.slug == ""


def test_export_and_scope():
    svc = _svc(isolation=SchemaIsolation())
    t = run(svc.provision("acme", "Acme"))
    exported = run(svc.export(t))
    assert exported["tenant"]["slug"] == "acme"
    assert exported["scope"] == {"mode": "schema", "schema": "t_acme"}


def test_update_config_and_branding():
    svc = _svc()
    t = run(svc.provision("acme", "Acme"))
    run(svc.update_config(t, {"locale": "ar", "timezone": "Asia/Riyadh"}))
    run(svc.set_branding(t, {"logo": "x.png", "colors": {"primary": "#0af"}}))
    reloaded = run(svc.get(t.id))
    assert reloaded.config["locale"] == "ar"
    assert reloaded.branding["logo"] == "x.png"
