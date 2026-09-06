"""AI connections with tool declarations — validation, per-connection isolation, the invoke loop,
and usage accumulation across round trips. Backward-compatible: an untouched connection is text-only.
"""
from __future__ import annotations

import asyncio
import itertools

import pytest

from app.adapters.datastore.memory import MemoryDataStore
from app.core.connections import (ConnectionError, ConnectionStore, invoke_conversation)


def run(c):
    return asyncio.run(c)


def _store(st=None):
    ids = (f"c{i}" for i in itertools.count(1))
    return ConnectionStore(st or MemoryDataStore(), now=lambda: "2026-09-06T00:00:00Z",
                           new_id=lambda: next(ids))


TOOLS = [{"name": "book", "description": "book a slot",
          "parameters": {"type": "object", "properties": {"day": {"type": "string"}}}}]


# ── validation ────────────────────────────────────────────────────────────────

def test_tools_default_empty_and_a_created_connection_keeps_them():
    s = _store()
    c = run(s.create(tenant="t1", name="plain", url="u"))
    assert c.tools == []
    c2 = run(s.create(tenant="t1", name="booker", url="u", tools=TOOLS))
    assert c2.tools[0]["name"] == "book"
    assert run(s.get("t1", c2.id)).tools[0]["name"] == "book"   # persisted


def test_a_tool_without_a_name_is_refused():
    s = _store()
    with pytest.raises(ConnectionError):
        run(s.create(tenant="t1", name="x", url="u", tools=[{"description": "no name"}]))


def test_duplicate_tool_names_are_refused():
    s = _store()
    with pytest.raises(ConnectionError):
        run(s.create(tenant="t1", name="x", url="u",
                     tools=[{"name": "book"}, {"name": "book"}]))


# ── update (tools are updatable) ──────────────────────────────────────────────

def test_update_replaces_tools_and_leaves_other_fields():
    s = _store()
    c = run(s.create(tenant="t1", name="x", url="u", input_price_micros=100))
    upd = run(s.update("t1", c.id, {"tools": TOOLS, "system_prompt": "be brief"}))
    assert upd.tools[0]["name"] == "book" and upd.system_prompt == "be brief"
    assert upd.input_price_micros == 100          # untouched
    assert run(s.get("t1", c.id)).tools[0]["name"] == "book"


def test_update_of_a_missing_connection_returns_none():
    s = _store()
    assert run(s.update("t1", "nope", {"tools": TOOLS})) is None


def test_update_rejects_a_bad_tool():
    s = _store()
    c = run(s.create(tenant="t1", name="x", url="u"))
    with pytest.raises(ConnectionError):
        run(s.update("t1", c.id, {"tools": [{"description": "no name"}]}))


# ── invoke_conversation: text, tool_call, usage accumulation, isolation ──────

def _converser(results):
    """A fake converse that returns queued results and records what tools/messages it saw."""
    seen = {"calls": []}
    q = list(results)

    async def converse(*, system, messages, tools, max_tokens):
        seen["calls"].append({"system": system, "messages": messages, "tools": tools})
        return q.pop(0)
    return converse, seen


def test_invoke_returns_text_and_meters_usage():
    s = _store()
    c = run(s.create(tenant="t1", name="x", url="u", tools=TOOLS,
                     input_price_micros=100, output_price_micros=300))
    conv, _ = _converser([{"kind": "text", "text": "hello", "tokens_in": 1000, "tokens_out": 1000}])
    out = run(invoke_conversation(c, messages="hi", converse=conv, store=s))
    assert out["kind"] == "text" and out["text"] == "hello"
    assert out["cost_micros"] == 100 + 300          # 1k in @100 + 1k out @300
    u = run(s.usage("t1", c.id))
    assert u["calls"] == 1 and u["cost_micros"] == 400


def test_invoke_returns_a_tool_call_shape():
    s = _store()
    c = run(s.create(tenant="t1", name="x", url="u", tools=TOOLS))
    conv, _ = _converser([{"kind": "tool_call",
                           "calls": [{"id": "1", "name": "book", "arguments": {"day": "wed"}}],
                           "tokens_in": 5, "tokens_out": 2}])
    out = run(invoke_conversation(c, messages=[{"role": "user", "content": "book wed"}],
                                  converse=conv, store=s))
    assert out["kind"] == "tool_call" and out["calls"][0]["name"] == "book"
    assert "text" not in out


def test_usage_accumulates_across_round_trips_in_one_turn():
    s = _store()
    c = run(s.create(tenant="t1", name="x", url="u", tools=TOOLS,
                     input_price_micros=1000, output_price_micros=1000))
    conv, _ = _converser([
        {"kind": "tool_call", "calls": [{"id": "1", "name": "book", "arguments": {}}],
         "tokens_in": 1000, "tokens_out": 1000},
        {"kind": "text", "text": "booked", "tokens_in": 1000, "tokens_out": 1000},
    ])
    run(invoke_conversation(c, messages="book", converse=conv, store=s))         # round 1: tool_call
    run(invoke_conversation(c, messages="book", converse=conv, store=s))         # round 2: text
    u = run(s.usage("t1", c.id))
    assert u["calls"] == 2 and u["cost_micros"] == 4000          # both round trips billed


def test_only_this_connections_tools_are_sent():
    s = _store()
    c = run(s.create(tenant="t1", name="x", url="u", tools=TOOLS))
    conv, seen = _converser([{"kind": "text", "text": "ok", "tokens_in": 0, "tokens_out": 0}])
    run(invoke_conversation(c, messages="hi", converse=conv, store=s))
    assert seen["calls"][0]["tools"] == TOOLS          # exactly this connection's tools, no others
    assert seen["calls"][0]["system"] == c.system_prompt
