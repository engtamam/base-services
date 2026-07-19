"""Deleting a data target or a RoPA entry — the two teardown paths that had no route.

Both took only an id. Exposing them unchanged would have made each a cross-tenant delete, and for
`delete_target` that is not an ordinary authorization bug: the data map DRIVES the erasure
fan-out, so removing one tenant's target means the next DSAR reaches fewer stores and still
reports success. Quietly narrowing erasure is the worst thing this service can do.
"""
from __future__ import annotations

import asyncio

import pytest

from app.adapters.datastore.memory import MemoryDataStore
from app.core.compliance_service import ComplianceService
from app.core.stores import RopaStore, TargetStore


def run(c):
    return asyncio.run(c)


class _Clock:
    def now(self):
        return 1_700_000_000.0


def _svc():
    store = MemoryDataStore()
    svc = ComplianceService.__new__(ComplianceService)
    svc._targets = TargetStore(store)
    svc._ropa = RopaStore(store)
    svc._clock = _Clock()
    svc._cfg = {"list_limit": 100, "residency_default": "sa"}
    svc._emitted = []

    async def _emit(name, payload):
        svc._emitted.append((name, payload))

    svc._emit = _emit
    return svc


# ── data targets ───────────────────────────────────────────────────────────────
def test_a_tenant_can_delete_its_own_target():
    svc = _svc()
    t = run(svc.register_target(tenant="acme", service="files", category="uploads"))
    assert run(svc.delete_target("acme", t.id)) is True
    assert run(svc.list_targets("acme")) == []


def test_ANOTHER_tenant_cannot_delete_it():
    """The one that matters. A target removed from acme's map by globex means acme's next DSAR
    silently stops reaching files-service — and still returns 'complete'."""
    svc = _svc()
    t = run(svc.register_target(tenant="acme", service="files", category="uploads"))

    assert run(svc.delete_target("globex", t.id)) is False
    assert len(run(svc.list_targets("acme"))) == 1        # still on the map


def test_a_foreign_target_is_indistinguishable_from_an_absent_one():
    """Both return False so the route answers 404 to each. A 403 for one and 404 for the other
    would turn the endpoint into an oracle for what other tenants hold."""
    svc = _svc()
    t = run(svc.register_target(tenant="acme", service="files", category="uploads"))
    assert run(svc.delete_target("globex", t.id)) == run(svc.delete_target("globex", "tgt_nope"))


def test_deleting_a_target_is_announced():
    """The map shrank. Whoever reasons about erasure completeness has to hear about it."""
    svc = _svc()
    t = run(svc.register_target(tenant="acme", service="files", category="uploads"))
    run(svc.delete_target("acme", t.id))
    assert svc._emitted[-1][0] == "data_target.deleted"
    assert svc._emitted[-1][1]["service"] == "files"      # WHICH store left the map


def test_deleting_twice_reports_absent_rather_than_succeeding_twice():
    svc = _svc()
    t = run(svc.register_target(tenant="acme", service="files", category="uploads"))
    assert run(svc.delete_target("acme", t.id)) is True
    assert run(svc.delete_target("acme", t.id)) is False


# ── RoPA ───────────────────────────────────────────────────────────────────────
def _activity(svc, tenant="acme"):
    return run(svc.create_activity(tenant=tenant, name="payroll", purpose="salary",
                                   lawful_basis="contract", data_categories=["financial"]))


def test_a_tenant_can_delete_its_own_activity():
    svc = _svc()
    a = _activity(svc)
    assert run(svc.delete_activity("acme", a.id)) is True
    assert run(svc.list_activities("acme")) == []


def test_another_tenant_cannot_delete_a_ropa_entry():
    svc = _svc()
    a = _activity(svc)
    assert run(svc.delete_activity("globex", a.id)) is False
    assert len(run(svc.list_activities("acme"))) == 1


def test_the_deletion_carries_what_the_entry_SAID():
    """Art. 30 obliges the controller to maintain the register. An entry vanishing with no trace
    is the outcome that cannot be defended, so the event carries the content, not just the id."""
    svc = _svc()
    a = _activity(svc)
    run(svc.delete_activity("acme", a.id))
    name, payload = svc._emitted[-1]
    assert name == "ropa_activity.deleted"
    assert payload["name"] == "payroll"
    assert payload["lawful_basis"] == "contract"
    assert payload["purpose"] == "salary"


def test_deleting_an_unknown_activity_is_false_not_an_error():
    assert run(_svc().delete_activity("acme", "ropa_nope")) is False
