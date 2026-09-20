"""KB entitlement gating — the second visibility axis (requires_entitlement), resolved server-side.

Security is the point: an entitlement-gated (detailed) entry is invisible unless the tenant genuinely
HOLDS the feature, the held set is resolved server-side (never caller-supplied), and everything
fails closed — no resolver, an empty set, or a resolver error all hide the gated entry, never reveal it.
"""
from __future__ import annotations

import asyncio

from app.adapters.entitlements.null import NullEntitlements
from app.adapters.knowledge_source.memory import MemoryKnowledgeSource
from app.adapters.model.extractive import ExtractiveModel
from app.core.kb import parse_entry
from app.core.kb_service import KbService

CFG = {"chunk_max_chars": 900, "top_n": 6, "min_score": 0.0, "max_context_chars": 6000,
       "bm25_k1": 1.2, "bm25_b": 0.75, "max_answer_tokens": 700}

# A general marketing-tier entry (no gate), a detailed entry gated on feature.loyalty, and a
# role-gated admin entry — three independent visibility situations.
DOC = {
    "loyalty.pitch": {"title": "Loyalty program", "body": "Earn points and reward returning customers."},
    "loyalty.howto": {"title": "Set up loyalty", "body": "detailed loyalty configuration usage steps",
                      "requires_entitlement": "feature.loyalty"},
    "admin.purge": {"title": "Purge", "body": "deletes permanently", "requires": "records.purge"},
}


def run(c):
    return asyncio.run(c)


class FakeResolver:
    """Per-tenant held sets — the authoritative source assistant-service resolves against."""
    driver = "fake"

    def __init__(self, mapping):
        self._m = mapping

    async def held(self, tenant):
        return frozenset(self._m.get(tenant, ()))


class BoomResolver:
    driver = "boom"

    async def held(self, tenant):
        raise RuntimeError("resolver down")


def _svc(resolver=None, doc=DOC):
    return KbService(source=MemoryKnowledgeSource(raw=doc), model=ExtractiveModel(), cfg=CFG,
                     entitlements=resolver)


def _ids(hits):
    return {h.entry.id for h in hits}


# ── the model: visible_to on both axes ───────────────────────────────────────────
def test_parse_defaults_entitlement_to_empty():
    e = parse_entry("x", {"title": {"en": "t"}, "body": {"en": "b"}})
    assert e.requires_entitlement == ""


def test_both_axes_are_required_when_both_are_set():
    e = parse_entry("x", {"title": {"en": "t"}, "body": {"en": "b"},
                          "requires": "records.purge", "requires_entitlement": "feature.loyalty"})
    assert e.visible_to(frozenset(), frozenset()) is False
    assert e.visible_to(frozenset({"records.purge"}), frozenset()) is False          # cap only
    assert e.visible_to(frozenset(), frozenset({"feature.loyalty"})) is False        # entitlement only
    assert e.visible_to(frozenset({"records.purge"}), frozenset({"feature.loyalty"})) is True


def test_a_general_entry_is_visible_with_nothing_held():
    e = parse_entry("g", {"title": {"en": "t"}, "body": {"en": "b"}})
    assert e.visible_to(frozenset(), frozenset()) is True


# ── retrieval: gated entry hidden until the tenant holds it ───────────────────────
def test_gated_entry_hidden_without_the_entitlement():
    svc = _svc(FakeResolver({}))
    hits = run(svc.retrieve(question="detailed loyalty configuration usage steps", surface="",
                            caps=frozenset(), lang="en", tenant="t1"))
    assert "loyalty.howto" not in _ids(hits)


def test_gated_entry_visible_when_the_tenant_holds_it():
    svc = _svc(FakeResolver({"t1": {"feature.loyalty"}}))
    hits = run(svc.retrieve(question="detailed loyalty configuration usage steps", surface="",
                            caps=frozenset(), lang="en", tenant="t1"))
    assert "loyalty.howto" in _ids(hits)


def test_the_general_pitch_is_always_findable():
    svc = _svc(FakeResolver({}))
    hits = run(svc.retrieve(question="loyalty program earn points", surface="",
                            caps=frozenset(), lang="en", tenant="t1"))
    assert "loyalty.pitch" in _ids(hits)                    # marketing tier shows to everyone


def test_resolution_is_per_tenant_and_server_side():
    # ONE resolver, TWO tenants: only the holder sees the detailed entry. A caller cannot change this
    # from the request — visibility is decided by the tenant's server-resolved held set.
    svc = _svc(FakeResolver({"holder": {"feature.loyalty"}}))
    q = dict(question="detailed loyalty configuration usage steps", surface="", caps=frozenset(), lang="en")
    assert "loyalty.howto" in _ids(run(svc.retrieve(tenant="holder", **q)))
    assert "loyalty.howto" not in _ids(run(svc.retrieve(tenant="other", **q)))


# ── fail-closed: no resolver / null / error all HIDE the gated entry ──────────────
def test_no_resolver_hides_gated_entries():
    svc = _svc(None)                                        # feature off entirely
    hits = run(svc.retrieve(question="detailed loyalty configuration usage steps", surface="",
                            caps=frozenset(), lang="en", tenant="t1"))
    assert "loyalty.howto" not in _ids(hits)


def test_null_resolver_hides_gated_entries_and_is_reached():
    r = NullEntitlements()
    svc = _svc(r)
    hits = run(svc.retrieve(question="detailed loyalty configuration usage steps", surface="",
                            caps=frozenset(), lang="en", tenant="t1"))
    assert "loyalty.howto" not in _ids(hits)
    assert r.asked == ["t1"]                                # resolver was consulted, not skipped


def test_a_resolver_error_hides_gated_entries_and_does_not_crash():
    svc = _svc(BoomResolver())
    hits = run(svc.retrieve(question="detailed loyalty configuration usage steps", surface="",
                            caps=frozenset(), lang="en", tenant="t1"))
    assert "loyalty.howto" not in _ids(hits)                # fail-closed on error
    # the ask still works for general content despite the resolver being down
    ok = run(svc.retrieve(question="loyalty program earn points", surface="",
                          caps=frozenset(), lang="en", tenant="t1"))
    assert "loyalty.pitch" in _ids(ok)


# ── the ? lookup path honours entitlement too ────────────────────────────────────
def test_anchor_lookup_respects_entitlement():
    doc = {"card": {"title": "Loyalty card", "body": "b", "anchor": "loyalty.card",
                    "requires_entitlement": "feature.loyalty"}}
    svc = _svc(FakeResolver({"holder": {"feature.loyalty"}}), doc=doc)
    assert run(svc.lookup(anchor="loyalty.card", surface="", caps=frozenset(), tenant="other")) is None
    got = run(svc.lookup(anchor="loyalty.card", surface="", caps=frozenset(), tenant="holder"))
    assert got is not None and got.id == "card"


def test_ask_abstains_on_a_gated_topic_the_tenant_has_not_bought():
    # end to end: a non-holder asking about the detailed feature gets no gated evidence.
    svc = _svc(FakeResolver({}))
    _answer, hits = run(svc.ask(question="detailed loyalty configuration usage steps", surface="",
                                caps=frozenset(), lang="en", tenant="t1",
                                not_found_text="I don't know"))
    assert "loyalty.howto" not in _ids(hits)
