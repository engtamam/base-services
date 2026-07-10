"""Consent: default-deny, purpose limitation, immediate withdrawal, expiry, append-only proof."""
from __future__ import annotations

import asyncio

from app.adapters.cache.memory import MemoryCache
from app.adapters.clock.manual import ManualClock
from app.adapters.datastore.memory import MemoryDataStore
from app.core.consent_service import ConsentService
from app.core.stores import ConsentStore


def run(c):
    return asyncio.run(c)


def _svc(st=None, clock=None, cache=None):
    return ConsentService(store=ConsentStore(st or MemoryDataStore()),
                          clock=clock or ManualClock(1_000_000.0), cache=cache)


def test_unknown_subject_is_denied_by_default():
    svc = _svc()
    r = run(svc.check(tenant="t1", subject="ghost", purpose="marketing"))
    assert r["allowed"] is False and r["reason"] == "no_consent_record"


def test_grant_then_allowed():
    svc = _svc()
    run(svc.grant(tenant="t1", subject="u1", purpose="marketing", notice_version="v2"))
    r = run(svc.check(tenant="t1", subject="u1", purpose="marketing"))
    assert r["allowed"] is True and r["notice_version"] == "v2"


def test_purpose_limitation_consent_does_not_generalise():
    svc = _svc()
    run(svc.grant(tenant="t1", subject="u1", purpose="marketing"))
    assert run(svc.check(tenant="t1", subject="u1", purpose="analytics"))["allowed"] is False


def test_withdrawal_is_immediate_even_when_cached():
    st, cache = MemoryDataStore(), MemoryCache()
    svc = _svc(st, cache=cache)
    run(svc.grant(tenant="t1", subject="u1", purpose="marketing"))
    assert run(svc.check(tenant="t1", subject="u1", purpose="marketing"))["allowed"] is True
    assert run(svc.check(tenant="t1", subject="u1", purpose="marketing"))["cached"] is True
    run(svc.withdraw(tenant="t1", subject="u1", purpose="marketing"))
    r = run(svc.check(tenant="t1", subject="u1", purpose="marketing"))
    assert r["allowed"] is False and r["reason"] == "withdrawn" and r["cached"] is False


def test_expired_consent_is_denied():
    clk = ManualClock(1_000.0)
    svc = _svc(clock=clk)
    run(svc.grant(tenant="t1", subject="u1", purpose="marketing", expires_at=2_000.0))
    assert run(svc.check(tenant="t1", subject="u1", purpose="marketing"))["allowed"] is True
    clk.set(2_500.0)
    r = run(svc.check(tenant="t1", subject="u1", purpose="marketing"))
    assert r["allowed"] is False and r["reason"] == "expired"


def test_consent_is_tenant_scoped():
    svc = _svc()
    run(svc.grant(tenant="t1", subject="u1", purpose="marketing"))
    assert run(svc.check(tenant="t2", subject="u1", purpose="marketing"))["allowed"] is False


def test_proof_is_append_only_history():
    svc = _svc()
    run(svc.grant(tenant="t1", subject="u1", purpose="marketing", notice_version="v1", method="web"))
    run(svc.withdraw(tenant="t1", subject="u1", purpose="marketing"))
    run(svc.grant(tenant="t1", subject="u1", purpose="marketing", notice_version="v2"))
    hist = run(svc.history("t1", "u1"))
    assert [p["action"] for p in hist["proofs"]] == ["granted", "withdrawn", "granted"]
    assert hist["proofs"][0]["notice_version"] == "v1"     # earlier proof never rewritten


def test_withdraw_without_prior_grant_is_safe():
    svc = _svc()
    run(svc.withdraw(tenant="t1", subject="u1", purpose="marketing"))
    assert run(svc.check(tenant="t1", subject="u1", purpose="marketing"))["allowed"] is False


def test_cache_outage_never_grants_access():
    class BrokenCache:
        driver = "broken"

        async def get(self, key):
            raise RuntimeError("down")

        async def set(self, key, value, ttl_seconds=None):
            raise RuntimeError("down")

    svc = _svc(cache=BrokenCache())
    assert run(svc.check(tenant="t1", subject="ghost", purpose="x"))["allowed"] is False
    run(svc.grant(tenant="t1", subject="u1", purpose="x"))
    assert run(svc.check(tenant="t1", subject="u1", purpose="x"))["allowed"] is True
