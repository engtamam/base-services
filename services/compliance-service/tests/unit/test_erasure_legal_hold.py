"""Erasure must honour a legal hold IN THE ERASURE ITSELF — not only in the DSAR orchestrator.

The hole: /internal/privacy/erase calls ComplianceErasure.erase() directly; the hold check lived
only in DsarService.process(), so a direct caller deleted data under an active legal hold.
"""
from __future__ import annotations

import asyncio
import time

from app.adapters.datastore.memory import MemoryDataStore
from app.core.erasure import ComplianceErasure


def run(c):
    return asyncio.run(c)


def _seed_consent(st, tenant="acme", subject="u1", purpose="marketing"):
    run(st.put("consent", f"{tenant}::{subject}::{purpose}",
               {"tenant": tenant, "subject": subject, "purpose": purpose, "updated_at": 1.0}))


def _seed_hold(st, tenant="acme", subject="u1", *, active=True, expires_at=0.0):
    run(st.put("legal_holds", "h1", {"id": "h1", "tenant": tenant, "subject": subject,
                                     "active": active, "expires_at": expires_at, "placed_at": 1.0}))


def test_an_active_hold_blocks_erasure_and_keeps_the_consent():
    st = MemoryDataStore()
    _seed_consent(st)
    _seed_hold(st, active=True)
    e = ComplianceErasure(store=st, pii_salt="pepper")
    out = run(e.erase("acme", "u1"))
    assert out["blocked_by_legal_hold"] is True and out["erased"] == []
    # the consent row is UNTOUCHED
    assert run(st.get("consent", "acme::u1::marketing")) is not None


def test_a_released_hold_does_not_block_erasure():
    st = MemoryDataStore()
    _seed_consent(st)
    _seed_hold(st, active=False)
    e = ComplianceErasure(store=st, pii_salt="pepper")
    out = run(e.erase("acme", "u1"))
    assert not out.get("blocked_by_legal_hold")
    assert "consent:1" in out["erased"]
    assert run(st.get("consent", "acme::u1::marketing")) is None


def test_an_expired_hold_does_not_block_erasure():
    st = MemoryDataStore()
    _seed_consent(st)
    _seed_hold(st, active=True, expires_at=time.time() - 100)   # expired
    e = ComplianceErasure(store=st, pii_salt="pepper")
    out = run(e.erase("acme", "u1"))
    assert not out.get("blocked_by_legal_hold") and "consent:1" in out["erased"]
