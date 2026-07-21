"""Authored help and generated help in one KB."""
from __future__ import annotations

import asyncio

import pytest

from app.adapters.knowledge_source.composite import CompositeKnowledgeSource
from app.adapters.knowledge_source.memory import MemoryKnowledgeSource


def run(c):
    return asyncio.run(c)


class _Broken:
    driver = "broken"
    async def load(self): raise RuntimeError("source down")
    async def health(self): return False
    async def close(self): return None


def test_entries_from_every_source_are_merged():
    a = MemoryKnowledgeSource(raw={"a": {"title": "A", "body": "B"}})
    b = MemoryKnowledgeSource(raw={"b": {"title": "A", "body": "B"}})
    assert {e.id for e in run(CompositeKnowledgeSource([a, b]).load()).entries} == {"a", "b"}


def test_the_first_source_wins_so_authored_help_overrides_generated():
    authored = MemoryKnowledgeSource(raw={"action.pay": {"title": "Authored", "body": "Human."}})
    generated = MemoryKnowledgeSource(raw={"action.pay": {"title": "Generated", "body": "Auto."}})
    snap = run(CompositeKnowledgeSource([authored, generated]).load())
    assert snap.entries[0].text("en")[0] == "Authored"


def test_a_failing_source_fails_the_whole_load():
    # Serving what we could reach means answering confidently while half the knowledge is missing,
    # with nothing in the response saying so.
    ok = MemoryKnowledgeSource(raw={"a": {"title": "A", "body": "B"}})
    with pytest.raises(RuntimeError):
        run(CompositeKnowledgeSource([ok, _Broken()]).load())


def test_the_generation_changes_when_any_source_changes():
    a = MemoryKnowledgeSource(raw={"a": {"title": "A", "body": "B"}})
    b1 = MemoryKnowledgeSource(raw={"b": {"title": "A", "body": "ONE"}})
    b2 = MemoryKnowledgeSource(raw={"b": {"title": "A", "body": "TWO"}})
    assert run(CompositeKnowledgeSource([a, b1]).load()).generation \
        != run(CompositeKnowledgeSource([a, b2]).load()).generation


def test_health_is_false_if_any_source_is_unhealthy():
    ok = MemoryKnowledgeSource(raw={"a": {"title": "A", "body": "B"}})
    assert run(CompositeKnowledgeSource([ok]).health()) is True
    assert run(CompositeKnowledgeSource([ok, _Broken()]).health()) is False


def test_no_sources_is_a_configuration_error():
    with pytest.raises(ValueError):
        CompositeKnowledgeSource([])
