"""Empty must mean "he holds none". It must NEVER mean "I did not understand you".

Passing a role NAME where an id was expected used to resolve to nothing and return 200 with an
empty capability set — a correct answer to a question asked in the wrong vocabulary. Identity then
minted a token that could do nothing, and every screen 403'd with no error anywhere to explain why.
"""
from __future__ import annotations

import asyncio

import pytest

from app.adapters.datastore.memory import MemoryDataStore
from app.core.authz_service import (
    BY_ID,
    BY_NAME,
    IGNORE,
    REJECT,
    AuthzService,
    UnknownRole,
)
from app.core.stores import AssignmentStore, PolicyStore, RoleStore
from app.ports.errors import ConfigError


def run(c):
    return asyncio.run(c)


class Engine:
    driver = "e"


def build(**kw):
    st = MemoryDataStore()
    svc = AuthzService(roles=RoleStore(st), policies=PolicyStore(st),
                       assignments=AssignmentStore(st), engine=Engine(), **kw)
    return svc


def a_role(svc, tenant="acme", name="compliance-manager", caps=("dsar:read", "dsar:close")):
    return run(svc.create_role(tenant, name, list(caps)))


# ── a role reference may be an id OR a name ──────────────────────────────────
def test_a_role_resolves_by_id():
    svc = build()
    role = a_role(svc)
    caps = run(svc.effective("sara", "acme", roles=[role.id]))
    assert caps == {"dsar:read", "dsar:close"}


def test_a_role_resolves_by_NAME():
    """What the caller actually holds. Identity's membership row says `compliance-manager`, not a
    generated hex id — only authz ever knew that."""
    svc = build()
    a_role(svc)
    caps = run(svc.effective("sara", "acme", roles=["compliance-manager"]))
    assert caps == {"dsar:read", "dsar:close"}


# ── an unresolvable role is LOUD ─────────────────────────────────────────────
def test_an_unknown_role_raises_instead_of_returning_empty():
    """THE fix. Silence here mints a token that can do nothing."""
    svc = build()
    a_role(svc)
    with pytest.raises(UnknownRole):
        run(svc.effective("sara", "acme", roles=["no-such-role"]))


def test_a_role_from_another_tenant_is_unknown_here():
    """T3 through the role door: a copied role id must not import another company's privileges —
    and it must not silently grant nothing either. It is simply not a role in this tenant."""
    svc = build()
    theirs = a_role(svc, tenant="rival-corp", name="admin", caps=("everything:*",))
    with pytest.raises(UnknownRole):
        run(svc.effective("sara", "acme", roles=[theirs.id]))


def test_a_duplicate_name_in_another_tenant_does_not_leak():
    svc = build()
    a_role(svc, tenant="rival-corp", name="admin", caps=("everything:*",))
    a_role(svc, tenant="acme", name="admin", caps=("acme:read",))
    caps = run(svc.effective("sara", "acme", roles=["admin"]))
    assert caps == {"acme:read"}, "same name, different company, different meaning"


def test_ignore_mode_restores_the_old_silent_behaviour():
    """Available, and documented as the bug it is."""
    svc = build(unknown_role=IGNORE)
    caps = run(svc.effective("sara", "acme", roles=["no-such-role"]))
    assert caps == set()


def test_empty_still_means_he_holds_none():
    """The guard must fire ONLY on an unresolvable role. A person with a real role that grants
    nothing gets an empty set, and that is a correct, quiet answer."""
    svc = build()
    a_role(svc, name="observer", caps=())
    assert run(svc.effective("sara", "acme", roles=["observer"])) == set()
    assert run(svc.effective("sara", "acme", roles=[])) == set()


# ── lookup mode is config ────────────────────────────────────────────────────
def test_id_only_mode_refuses_a_name():
    svc = build(role_lookup=BY_ID)
    a_role(svc)
    with pytest.raises(UnknownRole):
        run(svc.effective("sara", "acme", roles=["compliance-manager"]))


def test_name_only_mode_refuses_an_id():
    svc = build(role_lookup=BY_NAME)
    role = a_role(svc)
    with pytest.raises(UnknownRole):
        run(svc.effective("sara", "acme", roles=[role.id]))


def test_bad_config_fails_at_boot():
    with pytest.raises(ConfigError):
        build(role_lookup="nmae")
    with pytest.raises(ConfigError):
        build(unknown_role="rejectt")
