"""Non-security MEDIUM remediation (verification-service):
  009  the claims endpoint ships a strict default rate-limit class out of the box.
  011  a registry.remember() failure on a VERIFIED identity is telemetered, not silently swallowed.
  013  two-eyes governs REFUSALS too (a single reviewer cannot unilaterally refuse under two-eyes).
  014  READYZ_TIMEOUT_SECONDS is a declared Settings field.
"""
from __future__ import annotations

import asyncio
import json

import pytest

from app.adapters.datastore.memory import MemoryDataStore
from app.config import Settings
from app.core.models import PENDING, REFUSED, Verification, new_id
from app.core.review import ReviewService
from app.core.stores import VerificationStore


def run(c):
    return asyncio.run(c)


class _Clock:
    def now(self): return 1000.0


def _pending(store, subject="u1"):
    v = Verification(id=new_id("ver_"), tenant="acme", subject=subject, claim_type="business",
                     verdict=PENDING, created_at=1.0)
    run(store.put(v))
    return v


# ── 013: two-eyes on the refuse path ──────────────────────────────────────────
def test_single_reviewer_cannot_unilaterally_refuse_under_two_eyes():
    store = VerificationStore(MemoryDataStore())
    v = _pending(store)
    r = ReviewService(store=store, clock=_Clock(), two_eyes=True)
    first = run(r.decide(v.id, tenant="acme", approve=False, reviewer="sub_a"))
    assert first.verdict == PENDING                 # one reviewer refusing is NOT a decision
    same = run(r.decide(v.id, tenant="acme", approve=False, reviewer="sub_a"))
    assert same.verdict == PENDING                  # refusing twice yourself is one pair of eyes
    second = run(r.decide(v.id, tenant="acme", approve=False, reviewer="sub_b"))
    assert second.verdict == REFUSED                # two distinct reviewers → the refusal completes


def test_single_reviewer_refuse_without_two_eyes_still_works():
    store = VerificationStore(MemoryDataStore())
    v = _pending(store)
    r = ReviewService(store=store, clock=_Clock(), two_eyes=False)
    out = run(r.decide(v.id, tenant="acme", approve=False, reviewer="sub_a"))
    assert out.verdict == REFUSED


# ── 009 / 014: config defaults ────────────────────────────────────────────────
def _s():
    return Settings(_env_file=None, INTERNAL_SERVICE_TOKEN="x" * 40)


def test_claims_ships_a_default_rate_limit_route():
    routes = json.loads(_s().RATELIMIT_ROUTES)
    claims = [r for r in routes if r.get("prefix") == "/api/v1/verification/claims"]
    assert claims and claims[0]["limit"] <= 20       # a low out-of-box budget


def test_readyz_timeout_is_a_declared_field():
    assert _s().READYZ_TIMEOUT_SECONDS == 5.0


# ── 011: remember() failure is telemetered ────────────────────────────────────
def test_registry_remember_failure_is_telemetered():
    from app.core.verification_service import VerificationService
    from app.ports import verifier as vp

    class _OKVerifier:
        driver = "business"
        async def verify(self, claim): return vp.Outcome(status=vp.VERIFIED, provider="business",
                                                         reference="ok")

    class _BrokenRegistry:
        async def remember(self, *a, **k): raise RuntimeError("registry down")
        async def seen(self, *a, **k): return False

    class _Telemetry:
        def __init__(self): self.events = []
        def event(self, name, data): self.events.append((name, data))

    tel = _Telemetry()
    st = MemoryDataStore()
    svc = VerificationService(store=VerificationStore(st), verifiers={"business": _OKVerifier()},
                              clock=_Clock(), pii_salt="s" * 32, registry=_BrokenRegistry(),
                              telemetry=tel)
    run(svc.verify(tenant="acme", subject="u1", claim_type="business", value="CR123"))
    assert any(n == "verification.registry_remember_failed" for n, _ in tel.events)
