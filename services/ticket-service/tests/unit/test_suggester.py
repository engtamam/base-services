"""The AI advisory layer — logged, non-binding, off by default (53.7)."""
from __future__ import annotations

import asyncio

from app.adapters.datastore.memory import MemoryDataStore
from app.adapters.suggestion.null import NullSuggestionProvider
from app.core.stores import EventStore, TicketStore
from app.core.suggester import TicketSuggester
from app.core.ticket_service import TicketService
from app.core.workflow import WorkflowRegistry
from app.ports.suggestion_provider import Draft, Triage

SALT = "s" * 32
TYPES = [{"key": "feedback", "label": {"en": "F"}, "statuses": ["new", "closed"], "start": "new",
          "transitions": {"new": ["closed"]}, "terminal": ["closed"], "required_fields": []},
         {"key": "support", "label": {"en": "S"}, "statuses": ["new", "closed"], "start": "new",
          "transitions": {"new": ["closed"]}, "terminal": ["closed"], "required_fields": []}]


def run(c):
    return asyncio.run(c)


class _Provider:
    driver = "fake"

    def __init__(self, triage=None, draft=None):
        self._triage = triage
        self._draft = draft
        self.calls = []

    async def triage(self, **kw):
        self.calls.append(kw)
        return self._triage

    async def draft(self, **kw):
        return self._draft

    async def health(self):
        return True


def _svc(store, provider, *, autotag=1.0):
    wf = WorkflowRegistry.from_config(TYPES)
    tickets, events = TicketStore(store), EventStore(store)
    suggester = TicketSuggester(provider=provider, events=events, tickets=tickets, workflow=wf,
                                autotag_min_confidence=autotag)
    return TicketService(tickets=tickets, events=events, workflow=wf, pii_salt=SALT,
                         suggester=suggester)


def test_no_ai_wired_records_nothing_and_the_ticket_opens_fine():
    st = MemoryDataStore()
    svc = _svc(st, NullSuggestionProvider())
    t = run(svc.open(tenant="acme", type="feedback", subject="hi", body="x", raiser="ada@x.com"))
    hist, _ = run(svc.history("acme", t.id))
    assert [e["kind"] for e in hist] == ["created"]      # no ai_suggested event


def test_a_suggestion_is_logged_but_not_applied():
    st = MemoryDataStore()
    provider = _Provider(triage=Triage(type="support", priority="high", tags=("billing",),
                                       confidence=0.9, summary="looks like a billing issue"))
    svc = _svc(st, provider, autotag=1.0)          # threshold 1.0 => never auto-apply
    t = run(svc.open(tenant="acme", type="feedback", subject="charged twice", body="help",
                     raiser="ada@x.com"))
    hist, _ = run(svc.history("acme", t.id))
    kinds = [e["kind"] for e in hist]
    assert "ai_suggested" in kinds
    sug = [e for e in hist if e["kind"] == "ai_suggested"][0]
    assert sug["actor_ref"] == "ai" and sug["data"]["type"] == "support"
    # the ticket itself is UNCHANGED — advice is not authority
    reloaded = run(svc.get("acme", t.id))
    assert reloaded.type == "feedback" and reloaded.tags == []


def test_autotag_applies_only_above_the_confidence_threshold():
    st = MemoryDataStore()
    provider = _Provider(triage=Triage(tags=("vip",), confidence=0.95))
    svc = _svc(st, provider, autotag=0.9)          # 0.95 >= 0.9 => apply the tag
    t = run(svc.open(tenant="acme", type="feedback", subject="x", body="y", raiser="ada@x.com"))
    reloaded = run(svc.get("acme", t.id))
    assert "vip" in reloaded.tags
    hist, _ = run(svc.history("acme", t.id))
    tagged = [e for e in hist if e["kind"] == "tagged"][0]
    assert tagged["actor_ref"] == "ai"             # clearly the machine's tag, not a person's


def test_ai_never_changes_status():
    """Even a high-confidence triage cannot move the ticket — status is human-only."""
    st = MemoryDataStore()
    svc = _svc(st, _Provider(triage=Triage(type="support", confidence=1.0)), autotag=0.0)
    t = run(svc.open(tenant="acme", type="feedback", subject="x", body="y", raiser="ada@x.com"))
    assert run(svc.get("acme", t.id)).status == "new"    # untouched


def test_suggest_reply_drafts_but_does_not_send():
    st = MemoryDataStore()
    svc = _svc(st, _Provider(draft=Draft(text="Sorry for the trouble — we've refunded you.",
                                         confidence=0.8)))
    t = run(svc.open(tenant="acme", type="feedback", subject="x", body="y", raiser="ada@x.com"))
    out = run(svc.suggest_reply("acme", t.id))
    assert out["supported"] is True and "refunded" in out["text"]
    # drafting adds no reply to the thread — the agent must send it themselves
    hist, _ = run(svc.history("acme", t.id))
    assert [e["kind"] for e in hist] == ["created"]


def test_suggest_reply_says_unsupported_with_no_ai():
    st = MemoryDataStore()
    svc = _svc(st, NullSuggestionProvider())
    t = run(svc.open(tenant="acme", type="feedback", subject="x", body="y", raiser="ada@x.com"))
    assert run(svc.suggest_reply("acme", t.id)) == {"supported": False}
