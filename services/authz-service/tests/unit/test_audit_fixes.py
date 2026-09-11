"""Regression test for the 2026-09-12 fleet-audit finding authz-service-02.

Caller-supplied ABAC attributes must NOT shadow the authoritative subject.id/action/resource.id.
(#03, the chunked body-limit fix, is validated end-to-end through the ASGI stack in
tests/integration/test_body_limit_chunked.py.)
"""
from __future__ import annotations

from app.adapters.policy.abac import AbacEngine


def test_caller_attributes_cannot_shadow_the_subject_identity():
    ctx = {
        "subject": "real-user",
        "action": "read",
        "resource": "doc-1",
        # a user-influenced attribute map trying to impersonate someone else
        "attributes": {"subject.id": "attacker", "action": "delete", "resource.id": "other-doc"},
    }
    base = AbacEngine._context(ctx)
    assert base["subject.id"] == "real-user"    # authoritative, not the caller's "attacker"
    assert base["action"] == "read"
    assert base["resource.id"] == "doc-1"
