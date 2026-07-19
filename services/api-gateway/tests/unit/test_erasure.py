"""Erasure — api-gateway. It routes; it does not remember.

The endpoint always answers "nothing here", so the test that matters is the last one: it fails the
moment the gateway grows a store, forcing the erasure decision into review instead of letting PII
accumulate behind an endpoint that still says "nothing here".
"""
from __future__ import annotations

import asyncio

from app.core.erasure import GatewayErasure

_SALT = "s" * 32


def run(c):
    return asyncio.run(c)


def test_answers_already_absent_with_a_pseudonym():
    out = run(GatewayErasure(pii_salt=_SALT).erase("acme", "u@example.com"))
    assert out["already_absent"] is True
    assert out["erased"] == [] and out["retained"] == []
    assert out["subject_ref"].startswith("sub_")
    assert "u@example.com" not in out["subject_ref"]


def test_it_reads_nothing():
    class _Exploding:
        async def find(self, *a, **k):
            raise AssertionError("the gateway owns no subject data to look for")

    run(GatewayErasure(store=_Exploding(), pii_salt=_SALT).erase("acme", "u@example.com"))


def test_the_answer_explains_itself():
    out = run(GatewayErasure(pii_salt=_SALT).erase("acme", "u@example.com"))
    assert "routes" in out["note"] and "pseudonyms" in out["note"]
