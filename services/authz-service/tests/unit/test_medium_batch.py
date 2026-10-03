"""Non-security MEDIUM remediation: wildcard-aware require_capability (authz-005) and name-stored
assignment roles resolving in _subject_caps (authz-012)."""
from __future__ import annotations

import asyncio

import pytest
from fastapi import HTTPException

from app.adapters.datastore.memory import MemoryDataStore
from app.adapters.policy.rbac import RbacEngine
from app.api.auth import Principal
from app.api.authz import require_any_capability, require_capability
from app.core.authz_service import AuthzService
from app.core.stores import AssignmentStore, PolicyStore, RoleStore


def run(c):
    return asyncio.run(c)


def _svc():
    s = MemoryDataStore()
    return AuthzService(roles=RoleStore(s), policies=PolicyStore(s), assignments=AssignmentStore(s),
                        engine=RbacEngine(True))


# ── authz-005: a wildcard holder passes require_capability ────────────────────────
def test_require_capability_honours_wildcard():
    dep = require_capability("platform.authz")
    p = Principal(subject="u", caps=frozenset({"*"}), tenant="t")
    assert run(dep(p)) is p                                    # not locked out

    dep2 = require_capability("platform.authz")
    p2 = Principal(subject="u", caps=frozenset({"platform.*"}), tenant="t")
    assert run(dep2(p2)) is p2


def test_require_capability_still_denies_unrelated():
    dep = require_capability("platform.authz")
    with pytest.raises(HTTPException):
        run(dep(Principal(subject="u", caps=frozenset({"billing.read"}), tenant="t")))


def test_require_any_honours_wildcard():
    dep = require_any_capability("platform.authz", "authz.manage")
    p = Principal(subject="u", caps=frozenset({"platform.*"}), tenant="t")
    assert run(dep(p)) is p


# ── authz-012: an assignment role stored by NAME resolves to its capabilities ─────
def test_subject_caps_resolves_a_name_stored_role():
    svc = _svc()
    run(svc.create_role("acme", "editor", ["docs.edit", "docs.read"]))
    run(svc.set_subject_roles("sara", "acme", roles=["editor"]))   # by NAME, not id
    caps = run(svc.effective("sara", "acme"))
    assert "docs.edit" in caps and "docs.read" in caps            # name-stored role no longer dropped
