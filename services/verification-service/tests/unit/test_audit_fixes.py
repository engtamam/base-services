"""Regression tests for verification-service audit findings.

#01 an unconfigured verdict_ok can never read an empty provider value as VERIFIED (fail-open).
#02 the async provider callback (resolve) is wired to an internal route, so a PENDING can succeed.
#03 the postgres adapter ships an index matching the created_at ORDER BY every query uses.
#04 IdentityRegistry.remember is atomic — concurrent verifications of one identity both counted.
#05 ReviewService.decide is atomic — concurrent two-eyes approvals accumulate, never clobber.
"""
from __future__ import annotations

import asyncio

import pytest

from app.adapters.datastore.memory import MemoryDataStore
from app.adapters.verifier.http import HttpVerifier
from app.core.models import ERROR, PENDING, VERIFIED, Verification, new_id
from app.core.review import ReviewService
from app.core.stores import IdentityRegistry, VerificationStore
from app.ports import verifier as vp


def run(c):
    return asyncio.run(c)


class _Clock:
    def now(self):
        return 1000.0


class _YieldStore(MemoryDataStore):
    """A datastore that yields the event loop inside get(), so two coroutines racing on the same
    key actually interleave under asyncio.gather (a plain MemoryDataStore never awaits, so a 'race'
    would run each coroutine to completion and exercise nothing)."""

    async def get(self, collection, key):
        await asyncio.sleep(0)
        return await super().get(collection, key)


# ── 01: unconfigured verdict_ok never verifies an empty provider value ────────
def test_empty_verdict_ok_is_not_a_fail_open():
    v = HttpVerifier("prov", {"verdict_path": "data.status"})   # verdict_ok NOT configured
    out = v._read_verdict({"data": {"status": ""}})             # provider answered an empty value
    assert out.status == ERROR                                   # pre-fix: VERIFIED (""=="")
    # a genuine, configured success value still works
    v2 = HttpVerifier("prov", {"verdict_path": "data.status", "verdict_ok": "OK"})
    assert v2._read_verdict({"data": {"status": "OK"}}).status == VERIFIED


# ── 02: the async callback route is wired to resolve() ────────────────────────
def test_resolve_callback_route_is_registered():
    from app.main import create_app
    import os
    os.environ.update(ENV="test", INTERNAL_SERVICE_TOKEN="t" * 40, PII_SALT="s" * 32)
    from app.config import get_settings
    get_settings.cache_clear()
    from fastapi.testclient import TestClient
    app = create_app()
    c = TestClient(app, raise_server_exceptions=False)
    # No token: a REGISTERED route runs its auth dependency and answers 401; an ABSENT route 404s.
    r = c.post("/internal/verification/resolve", json={"verification_id": "x", "verified": True})
    assert r.status_code == 401                                 # pre-fix: 404 (route absent)
    # With the token and an unknown id, the wired route reaches resolve() -> 404 NOT_FOUND.
    r2 = c.post("/internal/verification/resolve", headers={"X-Internal-Token": "t" * 40},
                json={"verification_id": "nope", "verified": True})
    assert r2.status_code == 404


# ── 03: postgres ships an index matching the created_at ORDER BY queries use ──
def test_postgres_indexes_created_at():
    from app.adapters.datastore.postgres import _INDEXES
    joined = " ".join(_INDEXES)
    # every VerificationStore query sorts by created_at; the ordering index expression must match the
    # find() ORDER BY exactly (jsonb `->`, COALESCE) or Postgres bitmap-scans + top-N sorts instead.
    assert "value->'created_at'" in joined                      # pre-fix: only a `created` index


# ── 04: concurrent remember() of one identity counts both ─────────────────────
def test_concurrent_remember_counts_both():
    reg = IdentityRegistry(_YieldStore())

    async def _race():
        return await asyncio.gather(reg.remember("clm_x", 1.0), reg.remember("clm_x", 2.0))

    a, b = run(_race())
    firsts = [r for r in (a, b) if r["first_seen"]]
    assert len(firsts) == 1                                     # exactly one first-seen
    assert run(_Yield_seen(reg, "clm_x")) == 2                  # both recorded, count not clobbered


async def _Yield_seen(reg, ref):
    row = await reg._store.get("identity_seen", ref)
    return int(row.get("times", 0))


# ── 05: concurrent two-eyes approvals accumulate to a decision ────────────────
def test_concurrent_two_eyes_approvals_converge():
    store = VerificationStore(_YieldStore())
    v = Verification(id=new_id("ver_"), tenant="acme", subject="sub_x", claim_type="business",
                     verdict=PENDING, created_at=1.0)
    run(store.put(v))
    r = ReviewService(store=store, clock=_Clock(), two_eyes=True)

    async def _race():
        return await asyncio.gather(
            r.decide(v.id, tenant="acme", approve=True, reviewer="rev_a"),
            r.decide(v.id, tenant="acme", approve=True, reviewer="rev_b"),
            return_exceptions=True)

    run(_race())
    final = run(store.get(v.id))
    # Two DIFFERENT reviewers each approved: two-eyes is satisfied and the record settles VERIFIED.
    # Pre-fix, each wrote its own single-element reviewers list (last-write-wins) and it stayed PENDING.
    assert final.verdict == VERIFIED
