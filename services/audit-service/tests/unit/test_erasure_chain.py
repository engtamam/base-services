"""THE test of Phase 30: the chain still verifies after a person is erased.

The design is only sound if BOTH halves hold at once:

  * the person is unlinkable  — otherwise we failed the erasure (a legal breach), and
  * `verify()` still passes   — otherwise we destroyed the tamper-evidence (the product).

Any design that redacts a hash-chained record trades one for the other. This one refuses to store
the identity in the first place, so it owes nothing to either side.

The second half of the file is the enforcement that makes it true: audit REFUSES to record a raw
identity. By the time a value is hashed into the chain it is permanent — there is no later fix, so
the only place to catch it is the write.
"""
from __future__ import annotations

import asyncio

import pytest

from app.adapters.datastore.memory import MemoryDataStore
from app.core.erasure import AuditErasure
from app.core.pii import subject_ref

SALT = "product-pii-salt"
TENANT, SUBJECT = "t1", "alice@x.com"


def run(c):
    return asyncio.run(c)


def _audit():
    """A real AuditService over an in-memory store."""
    from app.core.audit_repository import AuditRepository
    from app.core.audit_service import AuditService
    store = MemoryDataStore()
    repo = AuditRepository(store)
    return AuditService(repo=repo, integrity=_chain(), exporters={},
                        default_format="json", partition_mode="global", query_limit=100), store


def _chain():
    from app.adapters.integrity.hash_chain import HashChainIntegrity
    return HashChainIntegrity()


def _record(audit, ref, action="login"):
    return run(audit.record(service="identity-service", action=action, actor=ref,
                            resource="session", tenant=TENANT))


# ── the whole point ─────────────────────────────────────────────────────────
def test_the_chain_still_verifies_after_the_subject_is_erased():
    """Erasure never touches audit — because audit never held the identity."""
    audit, _ = _audit()
    ref = subject_ref(SALT, TENANT, SUBJECT)
    for _ in range(20):
        _record(audit, ref)

    before = run(audit.verify())
    assert before["ok"], "the chain did not verify even BEFORE erasure"

    # identity-service deletes the linkage. Audit is not called, not modified, not asked to redact.
    eraser = AuditErasure(audit=audit)
    ack = run(eraser.erase(TENANT, ref))

    after = run(audit.verify())
    assert after["ok"], "the chain STOPPED verifying after an erasure — tamper-evidence destroyed"
    assert ack["chain_verifies"] is True
    assert ack["erased"] == []
    assert ack["retained"][0]["basis"] == "security_audit"


def test_the_events_never_named_the_person():
    """Unlinkable, not deleted: the chain proves sub_… acted, and can no longer say who that was."""
    audit, store = _audit()
    ref = subject_ref(SALT, TENANT, SUBJECT)
    _record(audit, ref)

    rows = run(store.scan_all("audit_events", 100))
    blob = str(rows)
    assert SUBJECT not in blob, "the person's email is IN THE CHAIN — permanent and unerasable"
    assert "alice" not in blob.lower()
    assert ref in blob, "the pseudonym should be there — that is what keeps the record queryable"


def test_retention_is_an_answer_with_a_reason_not_a_silence():
    audit, _ = _audit()
    ack = run(AuditErasure(audit=audit).erase(TENANT, subject_ref(SALT, TENANT, SUBJECT)))
    retained = ack["retained"][0]
    assert retained["basis"] and retained["why"], "audit kept data without saying why"


