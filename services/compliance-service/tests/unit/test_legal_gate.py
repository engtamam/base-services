"""Post-auth access gate — required docs must be accepted, and a version bump forces re-acceptance.
Acceptance is a version-pinned consent, so the re-accept behaviour falls out of the consent check."""
from __future__ import annotations

import asyncio

import pytest

from app.adapters.clock.manual import ManualClock
from app.adapters.datastore.memory import MemoryDataStore
from app.core.capacity import CapacityPolicy
from app.core.consent_service import ConsentService
from app.core.legal_gate import LegalGate, LegalGateError
from app.core.legal_pages import LegalPageService
from app.core.stores import ConsentStore, LegalPageStore


def run(c):
    return asyncio.run(c)


_CONTENT = {"title": {"en": "Terms", "ar": "الشروط"},
            "blocks": [{"section": {"text": {"en": "Be nice.", "ar": "كن لطيفًا."}}}], "values": {}}


def _harness():
    ds = MemoryDataStore()
    clock = ManualClock(1_000.0)
    consents = ConsentService(store=ConsentStore(ds), clock=clock, cache=None,
                              capacity=CapacityPolicy())
    legal_store = LegalPageStore(ds)
    legal = LegalPageService(store=legal_store, clock=clock)
    gate = LegalGate(legal_store=legal_store, consents=consents, clock=clock)
    return legal, gate


def _publish(legal, doc_id, version, requires=True):
    run(legal.draft(tenant="t", doc_id=doc_id, version=version, content=_CONTENT,
                    requires_acceptance=requires))
    run(legal.publish(tenant="t", doc_id=doc_id, version=version))


def test_a_required_doc_is_outstanding_until_accepted():
    legal, gate = _harness()
    _publish(legal, "terms", "1.0")
    out = run(gate.outstanding(tenant="t", subject="u1"))
    assert [o["doc_id"] for o in out] == ["terms"]
    assert out[0]["reason"] == "never_accepted" and out[0]["current_version"] == "1.0"
    assert "view" in out[0]                              # carries the payload to render the prompt

    run(gate.accept(tenant="t", subject="u1", doc_id="terms"))
    assert run(gate.outstanding(tenant="t", subject="u1")) == []
    assert run(gate.outstanding(tenant="t", subject="u1")) == []


def test_a_version_bump_forces_reacceptance():
    legal, gate = _harness()
    _publish(legal, "terms", "1.0")
    run(gate.accept(tenant="t", subject="u1", doc_id="terms"))
    assert run(gate.outstanding(tenant="t", subject="u1")) == []

    _publish(legal, "terms", "2.0")                     # terms change
    out = run(gate.outstanding(tenant="t", subject="u1"))
    assert len(out) == 1 and out[0]["reason"] == "version_changed"
    assert out[0]["accepted_version"] == "1.0" and out[0]["current_version"] == "2.0"

    run(gate.accept(tenant="t", subject="u1", doc_id="terms"))
    assert run(gate.outstanding(tenant="t", subject="u1")) == []   # cleared at v2


def test_an_informational_doc_is_never_gated_on():
    legal, gate = _harness()
    _publish(legal, "changelog", "1.0", requires=False)
    assert run(gate.outstanding(tenant="t", subject="u1")) == []


def test_accepting_an_unpublished_doc_is_refused():
    legal, gate = _harness()
    with pytest.raises(LegalGateError):
        run(gate.accept(tenant="t", subject="u1", doc_id="ghost"))


def test_accepting_a_doc_that_does_not_require_it_is_refused():
    legal, gate = _harness()
    _publish(legal, "changelog", "1.0", requires=False)
    with pytest.raises(LegalGateError):
        run(gate.accept(tenant="t", subject="u1", doc_id="changelog"))


def test_acceptance_is_per_subject_and_per_tenant():
    legal, gate = _harness()
    _publish(legal, "terms", "1.0")
    run(gate.accept(tenant="t", subject="u1", doc_id="terms"))
    assert run(gate.outstanding(tenant="t", subject="u2"))   # u2 hasn't accepted
    assert run(gate.outstanding(tenant="other", subject="u1")) == []  # other tenant has no doc
