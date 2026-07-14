"""A deleted tenant must stay LEARNABLE.

The event bus is at-most-once. `tenant.deleted` fires once, into the void, and a peer that was
restarting never hears it. If the row is purged too, that peer can never find out: the internal
read 404s for ever, peers fail OPEN on 404 (an unknown tenant is an onboarding gap, not a
punishment), and a deleted tenant stays writable permanently.

A read is self-healing. A missed event is wrong for ever.
"""
from __future__ import annotations

import asyncio

import pytest

from app.adapters.datastore.memory import MemoryDataStore
from app.core.models import ACTIVE, DELETED, Tenant, new_id
from app.core.tenant_repository import TenantRepository
from app.core.tenant_service import PURGE, TOMBSTONE, TenantService
from app.ports.errors import ConfigError


def run(c):
    return asyncio.run(c)


class Bus:
    driver = "b"

    def __init__(self):
        self.published = []

    async def publish(self, topic, event):
        self.published.append(topic)


class Isolation:
    driver = "shared-row"

    def scope(self, tenant):
        return {"mode": "shared-row"}


class Resolver:
    driver = "r"


def build(mode=TOMBSTONE, keep=("created_at",)):
    st = MemoryDataStore()
    repo = TenantRepository(st)
    svc = TenantService(
        repo=repo, isolation=Isolation(), resolver=Resolver(), event_bus=Bus(),
        default_isolation="shared-row", default_locale="en", default_timezone="UTC",
        default_calendar="gregorian", slug_min=2, slug_max=64,
        delete_mode=mode, tombstone_fields=keep)
    return svc, repo, st


def a_tenant(repo, slug="acme"):
    t = Tenant(id=new_id(), slug=slug, name="Acme Ltd", status=ACTIVE,
               domains=["acme.example.com"], branding={"logo": "https://x/logo.png"})
    return run(repo.create(t))


# ── the tombstone ────────────────────────────────────────────────────────────
def test_a_deleted_tenant_is_still_readable():
    """THE fix. A peer that missed the event learns the truth by LOOKING."""
    svc, repo, _ = build()
    t = a_tenant(repo)

    res = run(svc.delete(t))
    assert res["purged"] is False

    found = run(repo.get(t.id))
    assert found is not None, "the tenant must not vanish — a purge is unlearnable"
    assert found.status == DELETED
    assert found.deleted_at, "and it must say WHEN, so a peer can reason about ordering"


def test_the_tombstone_is_not_a_copy_of_the_tenant():
    """A tenant delete is often an ERASURE. The marker proving they are gone must not itself be
    a copy of them."""
    svc, repo, _ = build()
    t = a_tenant(repo)
    run(svc.delete(t))

    stone = run(repo.get(t.id))
    assert stone.name == "", "the name is personal data; it does not survive the delete"
    assert stone.branding == {}, "nor the branding"
    assert stone.domains == [], "nor the domains"
    assert stone.id == t.id and stone.status == DELETED, "only the fact of deletion survives"


def test_the_slug_and_host_are_released():
    """The tenant no longer answers on its host, and the slug can be taken by someone new.
    Only the id-keyed marker survives."""
    svc, repo, _ = build()
    t = a_tenant(repo)
    run(svc.delete(t))

    assert run(repo.get_by_slug("acme")) is None
    assert run(repo.get_by_domain("acme.example.com")) is None
    # and the slug is genuinely free again
    again = Tenant(id=new_id(), slug="acme", name="New Acme")
    assert run(repo.create(again)).slug == "acme"


def test_delete_is_idempotent():
    """A DSAR retries. Deleting twice must not explode."""
    svc, repo, _ = build()
    t = a_tenant(repo)
    run(svc.delete(t))
    stone = run(repo.get(t.id))
    run(svc.delete(stone))
    assert run(repo.get(t.id)).status == DELETED


def test_the_event_is_still_emitted_as_a_hint():
    """The tombstone does not replace the event — it makes the event non-load-bearing. The event
    still saves peers a round trip when it does arrive."""
    st = MemoryDataStore()
    repo = TenantRepository(st)
    bus = Bus()
    svc = TenantService(repo=repo, isolation=Isolation(), resolver=Resolver(), event_bus=bus,
                        default_isolation="shared-row", default_locale="en",
                        default_timezone="UTC", default_calendar="gregorian",
                        slug_min=2, slug_max=64,
                        delete_mode=TOMBSTONE, tombstone_fields=("created_at",))
    run(svc.delete(a_tenant(repo)))
    assert "tenant.deleted" in bus.published


# ── purge stays available, eyes open ─────────────────────────────────────────
def test_purge_mode_still_destroys_the_row():
    svc, repo, _ = build(mode=PURGE)
    t = a_tenant(repo)
    res = run(svc.delete(t))
    assert res["purged"] is True
    assert run(repo.get(t.id)) is None


def test_an_unknown_delete_mode_fails_at_boot():
    """A typo must not silently pick a mode. `TENANT_DELETE_MODE=tombstonee` quietly purging is
    the bug this whole fix exists to remove, reintroduced by a spelling mistake."""
    with pytest.raises(ConfigError):
        build(mode="tombstonee")
