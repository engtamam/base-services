"""Unit tests: condition evaluator, rbac wildcard, effective caps, decisions."""
from __future__ import annotations

import asyncio

import pytest

from app.adapters.datastore.memory import MemoryDataStore
from app.adapters.policy.abac import AbacEngine
from app.adapters.policy.combined import CombinedEngine
from app.adapters.policy.rbac import RbacEngine, cap_matches
from app.core.authz_service import AuthzService
from app.core.conditions import evaluate
from app.core.stores import AssignmentStore, PolicyStore, RoleStore


def run(c):
    return asyncio.run(c)


# ── condition evaluator (safe, no code exec) ────────────────────────────────
def test_ops():
    ctx = {"a": 5, "s": "hello", "list": [1, 2, 3], "n": None}
    assert evaluate({"attr": "a", "op": "eq", "value": 5}, ctx)
    assert evaluate({"attr": "a", "op": "ne", "value": 4}, ctx)
    assert evaluate({"attr": "a", "op": "gt", "value": 3}, ctx)
    assert evaluate({"attr": "a", "op": "le", "value": 5}, ctx)
    assert evaluate({"attr": "a", "op": "in", "value": [1, 5, 9]}, ctx)
    assert evaluate({"attr": "list", "op": "contains", "value": 2}, ctx)
    assert evaluate({"attr": "s", "op": "startswith", "value": "hel"}, ctx)
    assert evaluate({"attr": "a", "op": "exists"}, ctx)
    assert not evaluate({"attr": "n", "op": "exists"}, ctx)


def test_ref_and_nesting():
    ctx = {"resource.owner": "u1", "subject.id": "u1", "role": "admin"}
    assert evaluate({"attr": "resource.owner", "op": "eq", "value": "$subject.id"}, ctx)
    assert evaluate({"all": [{"attr": "role", "op": "eq", "value": "admin"},
                             {"attr": "resource.owner", "op": "eq", "value": "$subject.id"}]}, ctx)
    assert evaluate({"any": [{"attr": "role", "op": "eq", "value": "x"},
                             {"attr": "role", "op": "eq", "value": "admin"}]}, ctx)
    assert evaluate({"not": {"attr": "role", "op": "eq", "value": "x"}}, ctx)


def test_injection_is_literal_and_unknown_op_fails_closed():
    # a value that looks like code is compared literally, never executed
    assert not evaluate({"attr": "x", "op": "eq", "value": "__import__('os')"}, {"x": "safe"})
    assert not evaluate({"attr": "x", "op": "exec", "value": "1"}, {"x": "1"})  # unknown op
    assert not evaluate({"attr": "x", "op": "gt", "value": "notnum"}, {"x": 5})  # type-safe


# ── rbac wildcard ────────────────────────────────────────────────────────────
def test_cap_matches():
    assert cap_matches("files.read", {"files.read"}, True)
    assert cap_matches("files.read", {"files.*"}, True)
    assert cap_matches("anything", {"*"}, True)
    assert not cap_matches("files.read", {"files.*"}, False)   # wildcard off
    assert not cap_matches("admin.x", {"files.*"}, True)


# ── AuthzService ─────────────────────────────────────────────────────────────
def _svc(driver="combined"):
    store = MemoryDataStore()
    engine = {"rbac": RbacEngine(True), "abac": AbacEngine(True),
              "combined": CombinedEngine(RbacEngine(True), AbacEngine(True))}[driver]
    return AuthzService(roles=RoleStore(store), policies=PolicyStore(store),
                        assignments=AssignmentStore(store), engine=engine)


def test_effective_caps_from_roles():
    svc = _svc()
    r = run(svc.create_role("t1", "editor", ["docs.*", "files.read"]))
    run(svc.set_subject_roles("u1", "t1", [r.id], capabilities=["extra.cap"]))
    caps = run(svc.effective("u1", "t1"))
    assert caps == {"docs.*", "files.read", "extra.cap"}


def test_check_rbac_allow_deny():
    svc = _svc()
    r = run(svc.create_role("t1", "editor", ["docs.*"]))
    run(svc.set_subject_roles("u1", "t1", [r.id]))
    assert run(svc.check(subject="u1", action="docs.edit", resource="d", tenant="t1")).allow
    assert not run(svc.check(subject="u1", action="admin.kill", resource="d", tenant="t1")).allow


def test_check_abac_deny_overrides():
    svc = _svc()
    r = run(svc.create_role("t1", "editor", ["docs.*"]))
    run(svc.set_subject_roles("u1", "t1", [r.id]))
    run(svc.create_policy("t1", "deny", "docs.*", "*",
                          condition={"attr": "resource.locked", "op": "eq", "value": True}))
    d = run(svc.check(subject="u1", action="docs.edit", resource="d", tenant="t1",
                      attributes={"resource.locked": True}))
    assert d.allow is False and d.matched_policy is not None
    # unlocked -> rbac still allows
    assert run(svc.check(subject="u1", action="docs.edit", resource="d", tenant="t1",
                         attributes={"resource.locked": False})).allow


def test_check_abac_allow_without_role():
    svc = _svc()
    run(svc.create_policy("t1", "allow", "reports.*", "*",
                          condition={"attr": "resource.owner", "op": "eq", "value": "$subject.id"}))
    d = run(svc.check(subject="u1", action="reports.view", resource="r", tenant="t1",
                      attributes={"resource.owner": "u1", "subject.id": "u1"}))
    assert d.allow is True
