"""Audit FIN-004 — wallet idempotency keys are length-prefixed so the tenant↔source boundary is
unambiguous even when a tenant id or a caller source contains the "|" delimiter, AND pre-upgrade
(unprefixed) markers/claims are still honored via a read-only legacy fallback (no double money op
across the deploy).
"""
from __future__ import annotations

import asyncio

from app.adapters.datastore.memory import MemoryDataStore
from app.core.wallet_service import WalletService

_IDEMPO = "wallet_idempo"
_CREDIT_SEEN = "wallet_credit_seen"
_SPEND_SEEN = "wallet_spend_seen"

CFG = {"funds": (("cash", 1, True, True),), "vat_bp": 0, "currency": "SAR", "hold_ttl": 900,
       "hold_sweep_limit": 500, "grant_sweep_limit": 500, "idempotency_ttl": 3600,
       "ledger_list_limit": 100, "cas_max_retries": 8, "idempotency_claim_lease_seconds": 60}


def run(c):
    return asyncio.run(c)


class Clock:
    def __init__(self, t=1000.0): self.t = t
    def now(self): return self.t


def _svc(st=None):
    return WalletService(store=st or MemoryDataStore(), clock=Clock(), config={**CFG})


def _cash(svc, tenant="t1"):
    return run(svc.balance(tenant))["funds"].get("cash", 0)


# ── the delimiter ambiguity is gone ─────────────────────────────────────────────────
def test_length_prefix_disambiguates_the_tenant_source_boundary():
    # The classic collision: ("a","b|c") and ("a|b","c") both build "a|b|c" unprefixed.
    assert WalletService._legacy_key("a", "b|c") == WalletService._legacy_key("a|b", "c")   # collides
    assert WalletService._pfx_key("a", "b|c") != WalletService._pfx_key("a|b", "c")         # fixed
    assert WalletService._pfx_key("a", "b|c") == "1:a|b|c"
    assert WalletService._pfx_key("a|b", "c") == "3:a|b|c"


def test_new_credit_writes_the_length_prefixed_marker_and_claim():
    st = MemoryDataStore(); svc = _svc(st)
    run(svc.credit(tenant="t1", fund="cash", amount=100, source="s1"))
    assert run(st.get(_CREDIT_SEEN, "2:t1|s1")) is not None      # length-prefixed marker
    assert run(st.get(_IDEMPO, "2:t1|credit|s1")) is not None    # length-prefixed claim
    assert run(st.get(_CREDIT_SEEN, "t1|s1")) is None            # never writes the legacy key


# ── migration: a pre-upgrade (legacy) marker/claim is still honored ──────────────────
def test_a_legacy_credit_marker_still_dedupes_no_double_credit():
    st = MemoryDataStore(); svc = _svc(st)
    # simulate a credit that landed BEFORE the upgrade: only the legacy-format marker exists
    run(st.put(_CREDIT_SEEN, "t1|s1",
               {"id": "t1|s1", "tenant": "t1", "source": "s1",
                "result": {"credited": 100, "fund": "cash"}}))
    out = run(svc.credit(tenant="t1", fund="cash", amount=100, source="s1"))
    assert out.get("idempotent") is True                         # replayed via the legacy fallback
    assert _cash(svc) == 0                                       # NOT credited a second time


def test_a_legacy_spend_marker_still_refuses_a_redelivery():
    st = MemoryDataStore(); svc = _svc(st)
    run(svc.credit(tenant="t1", fund="cash", amount=1000, source="seed"))
    run(st.put(_SPEND_SEEN, "t1|sp1",
               {"id": "t1|sp1", "tenant": "t1", "source": "sp1",
                "result": {"spent": 400, "plan": {"applied": {"cash": 400}}}}))
    out = run(svc.spend(tenant="t1", net=400, source="sp1"))
    assert out.get("idempotent") is True                         # replayed via legacy fallback
    assert _cash(svc) == 1000                                    # NOT debited again


def test_a_legacy_finished_claim_is_replayed():
    st = MemoryDataStore(); svc = _svc(st)
    run(st.put(_IDEMPO, "t1|credit|s9",
               {"result": {"credited": 55, "fund": "cash"}, "version": 1,
                "expires_at": 10_000_000_000}))
    out = run(svc.credit(tenant="t1", fund="cash", amount=55, source="s9"))
    assert out.get("idempotent") is True and _cash(svc) == 0     # replayed the legacy claim, no credit


# ── two genuinely distinct ops that once collided now stay independent ────────────────
def test_new_ops_that_would_collide_legacy_are_now_independent():
    st = MemoryDataStore(); svc = _svc(st)
    # ("t","a|b") and ("t|a","b") collide on the legacy seen key "t|a|b"; with prefixing they don't,
    # so both credits apply to their OWN tenant wallet (neither is suppressed as the other's replay).
    run(svc.credit(tenant="t", fund="cash", amount=100, source="a|b"))
    run(svc.credit(tenant="t|a", fund="cash", amount=50, source="b"))
    assert _cash(svc, "t") == 100 and _cash(svc, "t|a") == 50
