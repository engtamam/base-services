"""Erasure must honour a legal hold IN THE ERASURE ITSELF — not only in the DSAR orchestrator.

The hole: /internal/privacy/erase calls ComplianceErasure.erase() directly; the hold check lived
only in DsarService.process(), so a direct caller deleted data under an active legal hold.
"""
from __future__ import annotations

import asyncio

from app.adapters.datastore.memory import MemoryDataStore
from app.core.erasure import ComplianceErasure
from app.core.models import ConsentProof, new_id
from app.core.stores import ConsentStore


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


def test_a_stray_expires_at_does_not_lift_an_active_hold():
    # r2-10: LegalHold has NO expires_at field, so the old wall-clock expiry branch was dead code that
    # advertised a capability nothing populated. A hold ends ONLY by being released (active=False).
    # Fail closed: an active hold carrying a stray past expires_at STILL blocks — the `active` flag,
    # the same one every other hold path uses, is the only mechanism.
    st = MemoryDataStore()
    _seed_consent(st)
    _seed_hold(st, active=True, expires_at=1.0)   # long past — but the hold is still active
    e = ComplianceErasure(store=st, pii_salt="pepper")
    out = run(e.erase("acme", "u1"))
    assert out["blocked_by_legal_hold"] is True and out["erased"] == []
    assert run(st.get("consent", "acme::u1::marketing")) is not None


def test_an_active_hold_beyond_the_first_page_still_blocks_erasure():
    # r2-01 (CRITICAL), red on the prefix: the old seam read holds capped at limit=100 sorted
    # placed_at ASC and filtered `active` in Python — so a subject with 100+ old RELEASED holds and
    # one NEW active hold had the active hold fall beyond the page, and erase() deleted held data.
    # active_for_subject pushes {"active": True} into the store, so the active hold is found regardless
    # of how many released holds precede it.
    st = MemoryDataStore()
    _seed_consent(st)
    for i in range(150):                                   # 150 released holds, oldest first
        run(st.put("legal_holds", f"rel{i}", {"id": f"rel{i}", "tenant": "acme", "subject": "u1",
                                              "active": False, "placed_at": float(i)}))
    run(st.put("legal_holds", "live", {"id": "live", "tenant": "acme", "subject": "u1",
                                       "active": True, "placed_at": 999.0}))   # newest, active
    e = ComplianceErasure(store=st, pii_salt="pepper")
    out = run(e.erase("acme", "u1"))
    assert out["blocked_by_legal_hold"] is True and out["erased"] == []
    assert run(st.get("consent", "acme::u1::marketing")) is not None


def test_an_incomplete_hold_read_fails_closed():
    # r2-01: completeness is the safety property. If the hold list cannot be read completely, erase
    # must REFUSE (treat truncation as held) rather than proceed on a page that may hide a hold.
    class _Truncating(MemoryDataStore):
        async def find(self, collection, where, **kw):
            rows, _ = await super().find(collection, where, **kw)
            if collection == "legal_holds":
                return rows, "more"          # always claim there is another page
            return rows, None

    st = _Truncating()
    _seed_consent(st)
    # no active holds seeded at all — the ONLY reason to refuse is the incomplete (truncated) read
    e = ComplianceErasure(store=st, pii_salt="pepper")
    out = run(e.erase("acme", "u1"))
    assert out["blocked_by_legal_hold"] is True and out["hold_check_incomplete"] is True
    assert out["erased"] == []
    assert run(st.get("consent", "acme::u1::marketing")) is not None


def test_erasure_removes_the_raw_subject_from_consent_proof_seq():
    # r2-03: consent_proof_seq is keyed "tenant::<RAW subject>" — a surviving raw PII identifier that
    # was exported in backups after an erasure swore the person was gone. erase() must delete it.
    st = MemoryDataStore()
    _seed_consent(st)
    cs = ConsentStore(st)
    run(cs.add_proof(ConsentProof(id=new_id("prf_"), tenant="acme", subject="alice@example.com",
                                  purpose="marketing", action="granted", at=1.0)))
    run(st.put("consent", "acme::alice@example.com::marketing",
               {"tenant": "acme", "subject": "alice@example.com", "purpose": "marketing",
                "updated_at": 1.0}))
    assert run(st.get("consent_proof_seq", "acme::alice@example.com")) is not None   # precondition

    e = ComplianceErasure(store=st, pii_salt="pepper")
    out = run(e.erase("acme", "alice@example.com"))
    assert not out.get("blocked_by_legal_hold")
    # the raw-subject-keyed sequence row is GONE
    assert run(st.get("consent_proof_seq", "acme::alice@example.com")) is None
    # and no surviving consent_proof row still carries the raw subject
    survivors = run(st.scan_all("consent_proof", 1000))
    assert all(r["subject"] != "alice@example.com" for r in survivors)


def test_consent_deletion_pages_past_the_thousand_row_cap():
    # r2-04: consent deletion was capped at limit=1000 with no pagination while the ack still reported
    # success — survivors past the cap silently remained. It must page to completion.
    st = MemoryDataStore()
    for i in range(1050):
        run(st.put("consent", f"acme::u1::p{i:04d}",
                   {"id": f"acme::u1::p{i:04d}", "tenant": "acme", "subject": "u1",
                    "purpose": f"p{i:04d}", "updated_at": float(i)}))
    e = ComplianceErasure(store=st, pii_salt="pepper")
    out = run(e.erase("acme", "u1"))
    assert out["erased"] == ["consent:1050"]
    assert run(st.scan_all("consent", 5000)) == []        # nothing left behind


# ── compliance-002/004: erasure scrubs the service's own dsar rows + guardian PII ───────────────────
def test_erasure_scrubs_the_services_own_dsar_rows():
    from app.core.pii import subject_ref
    st = MemoryDataStore()
    run(st.put("dsar", "d1", {"id": "d1", "tenant": "acme", "subject": "u1", "type": "erasure",
                              "status": "completed", "payload": {"email": "u1@x.com"},
                              "created_at": 1.0}))
    e = ComplianceErasure(store=st, pii_salt="pepper")
    out = run(e.erase("acme", "u1"))
    row = run(st.get("dsar", "d1"))
    assert row["subject"] == subject_ref("pepper", "acme", "u1")   # raw id pseudonymised
    assert row["payload"] == {}                                    # request payload scrubbed
    assert row is not None                                         # the record itself survives
    assert out["already_absent"] is False


def test_erasure_pseudonymises_guardian_consented_by():
    from app.core.pii import subject_ref
    st = MemoryDataStore()
    run(st.put("consent_proof", "p1", {"id": "p1", "tenant": "acme", "subject": "child",
                                       "consented_by": "parent@x.com", "relation": "guardian",
                                       "at": 1.0}))
    e = ComplianceErasure(store=st, pii_salt="pepper")
    run(e.erase("acme", "child"))
    p = run(st.get("consent_proof", "p1"))
    assert p["subject"] == subject_ref("pepper", "acme", "child")
    assert p["consented_by"] == subject_ref("pepper", "acme", "parent@x.com")   # guardian id gone
    assert "parent@x.com" not in str(p)
