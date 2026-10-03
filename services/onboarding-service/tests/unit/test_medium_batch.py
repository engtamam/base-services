"""Non-security MEDIUM remediation (onboarding-service):
  008  a forced requirement EXPIRES (bounded TTL) and can be un-forced, so the live check resumes.
  009  a money-like grant without once_per is a FATAL boot error, not a warning.
  010  the evaluator asks independent sources CONCURRENTLY and dedupes by (source, fact).
"""
from __future__ import annotations

import asyncio

import pytest

from app.adapters.datastore.memory import MemoryDataStore
from app.core.local_facts import LocalFacts


def run(c):
    return asyncio.run(c)


class _Clock:
    def __init__(self, t=1000.0): self.t = t
    def now(self): return self.t


# ── 008: forced-fact TTL + unforce ────────────────────────────────────────────
def test_forced_fact_expires_and_live_check_resumes():
    clock = _Clock()
    lf = LocalFacts(MemoryDataStore(), clock, default_force_ttl=100.0)
    req = {"id": "kyc"}
    run(lf.force(tenant="t", subject="u", requirement="kyc", actor="op", reason="manual"))
    assert run(lf.check(fact="x", tenant="t", subject="u", req=req)).status == "satisfied"
    clock.t += 101                                 # force has expired
    ans = run(lf.check(fact="x", tenant="t", subject="u", req=req))
    assert ans.status != "satisfied"               # live check resumes (unknown local fact)


def test_unforce_resumes_the_live_check_immediately():
    lf = LocalFacts(MemoryDataStore(), _Clock(), default_force_ttl=0.0)
    req = {"id": "kyc"}
    run(lf.force(tenant="t", subject="u", requirement="kyc", actor="op", reason="manual"))
    assert run(lf.check(fact="x", tenant="t", subject="u", req=req)).status == "satisfied"
    assert run(lf.unforce(tenant="t", subject="u", requirement="kyc")) is True
    assert run(lf.check(fact="x", tenant="t", subject="u", req=req)).status != "satisfied"


def test_explicit_ttl_overrides_the_default():
    clock = _Clock()
    lf = LocalFacts(MemoryDataStore(), clock, default_force_ttl=10_000.0)
    exp = run(lf.force(tenant="t", subject="u", requirement="kyc", actor="op", reason="r",
                       ttl_seconds=5))
    assert exp == clock.now() + 5


# ── 009: money-like grant without once_per is fatal at boot ────────────────────
def _prod_settings(flows, **over):
    from app.config import Settings
    import app.onboarding_flows as of
    of.ONBOARDING_FLOWS = flows
    base = dict(_env_file=None, INTERNAL_SERVICE_TOKEN="x" * 40, ENV="production",
                PII_SALT="s" * 16, ONBOARDING_ENTITLEMENT="wallet",
                ONBOARDING_WALLET_URL="https://wallet.internal",
                ONBOARDING_GRANTS="welcome=amount:5000|currency:SAR")
    base.update(over)
    return Settings(**base)


def test_money_like_grant_without_once_per_is_fatal():
    flows = [{"flow": "signup", "audience": {}, "requirements": [
        {"id": "r1", "satisfied_by": "local.granted", "grants": "welcome"}]}]
    with pytest.raises(ValueError, match="once_per"):
        _prod_settings(flows).assert_onboarding_config_ok()


def test_money_like_grant_with_once_per_is_ok():
    flows = [{"flow": "signup", "audience": {}, "requirements": [
        {"id": "r1", "satisfied_by": "local.granted", "grants": "welcome",
         "once_per": "verified_identity"}]}]
    _prod_settings(flows).assert_onboarding_config_ok()   # no raise


# ── 010: concurrent, deduped source lookups ───────────────────────────────────
def test_evaluator_asks_each_source_once_and_concurrently():
    from app.core.evaluator import Evaluator
    from app.ports.requirement_source import SATISFIED, Answer

    order = []

    class _Source:
        def __init__(self): self.calls = 0
        async def check(self, *, fact, tenant, subject):
            self.calls += 1
            order.append(("start", fact))
            await asyncio.sleep(0.02)
            order.append(("end", fact))
            return Answer(status=SATISFIED)

    src = _Source()
    ev = Evaluator(sources={"identity": src})
    # two requirements sharing the SAME fact + one distinct fact
    flow = {"flow": "f", "requirements": [
        {"id": "a", "satisfied_by": "identity.email"},
        {"id": "b", "satisfied_by": "identity.email"},
        {"id": "c", "satisfied_by": "identity.phone"},
    ]}
    st = run(ev.evaluate(flow, tenant="t", subject="u"))
    assert all(r.status == "satisfied" for r in st.requirements)
    assert src.calls == 2                           # email asked ONCE (deduped) + phone once
    # concurrency: the two distinct lookups overlap (both start before either ends)
    starts = [i for i, e in enumerate(order) if e[0] == "start"]
    first_end = next(i for i, e in enumerate(order) if e[0] == "end")
    assert starts[1] < first_end                    # a second lookup started before the first ended
