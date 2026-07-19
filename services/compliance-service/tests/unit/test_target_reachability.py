"""The data map and PRIVACY_TARGETS are two halves of one fact, and nothing compared them.

A DataTarget could name a service that was never deployed. Registration accepted it silently, and
the mismatch surfaced only when a DSAR arrived: _url_for returns None, the target is marked
FAILED/UNREGISTERED, it burns PRIVACY_MAX_ATTEMPTS, and the DSAR can never complete. Weeks later,
on a statutory clock, discovered by whoever is on call rather than whoever made the typo.

Registration is the moment the operator still has the name in front of them.
"""
from __future__ import annotations

import asyncio

import pytest

from app.adapters.datastore.memory import MemoryDataStore
from app.core.compliance_service import ComplianceService
from app.core.stores import TargetStore


def run(c):
    return asyncio.run(c)


class _Clock:
    def now(self):
        return 1_700_000_000.0


def _svc(known=("identity-service", "file-service")):
    store = MemoryDataStore()
    svc = ComplianceService.__new__(ComplianceService)
    svc._targets = TargetStore(store)
    svc._clock = _Clock()
    svc._cfg = {"list_limit": 100, "residency_default": "sa", "known_targets": known}
    svc._emitted = []

    async def _emit(name, payload):
        svc._emitted.append((name, payload))

    svc._emit = _emit
    return svc


def test_a_configured_target_registers():
    svc = _svc()
    t = run(svc.register_target(tenant="acme", service="identity-service", category="profile"))
    assert t.service == "identity-service"


def test_an_unconfigured_target_is_REFUSED_at_registration():
    """THE fix. Before this it registered happily and jammed a DSAR weeks later."""
    svc = _svc()
    with pytest.raises(ValueError) as e:
        run(svc.register_target(tenant="acme", service="crm-service", category="contacts"))
    assert "crm-service" in str(e.value)


def test_the_refusal_names_the_targets_that_DO_exist():
    """The operator hitting this has a typo or an undeployed service. Both are fixed by seeing
    the real set, and neither is fixed by 'invalid target'."""
    svc = _svc()
    with pytest.raises(ValueError) as e:
        run(svc.register_target(tenant="acme", service="identtiy-service", category="profile"))
    msg = str(e.value)
    assert "identity-service" in msg and "file-service" in msg


def test_a_deployment_with_NO_configured_targets_still_accepts_registrations():
    """Local and test deployments have not configured the fan-out at all. Refusing everything
    there would break the setups least able to debug it — and production cannot reach this state,
    because assert_privacy_config_ok() refuses to boot on http transport with no targets."""
    svc = _svc(known=())
    t = run(svc.register_target(tenant="acme", service="anything", category="x"))
    assert t.service == "anything"


# ── drift, which registration-time validation cannot catch ─────────────────────
def test_a_target_that_became_unreachable_is_reported():
    """PRIVACY_TARGETS is config. A name valid at registration goes stale the moment a deployment
    changes and the map does not, and nothing re-checks it."""
    svc = _svc()
    run(svc.register_target(tenant="acme", service="file-service", category="uploads"))

    svc._cfg["known_targets"] = ("identity-service",)          # file-service was decommissioned
    out = run(svc.unreachable_targets("acme"))
    assert len(out) == 1
    assert out[0]["service"] == "file-service"


def test_reachable_targets_are_not_reported():
    svc = _svc()
    run(svc.register_target(tenant="acme", service="identity-service", category="profile"))
    assert run(svc.unreachable_targets("acme")) == []


def test_unreachable_is_empty_rather_than_everything_when_nothing_is_configured():
    """With no configured targets we do not KNOW anything is unreachable. Reporting every target
    as broken would be the same unknown-treated-as-a-fact error, pointed the other way."""
    svc = _svc(known=())
    run(svc.register_target(tenant="acme", service="whatever", category="x"))
    assert run(svc.unreachable_targets("acme")) == []


def test_it_is_scoped_to_the_calling_tenant():
    svc = _svc()
    run(svc.register_target(tenant="acme", service="file-service", category="uploads"))
    svc._cfg["known_targets"] = ("identity-service",)
    assert run(svc.unreachable_targets("globex")) == []
