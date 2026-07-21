"""The file knowledge source.

Sample entries only; real help content is authored separately, later.

The two behaviours worth pinning: a missing KB must be LOUD (an assistant that answers "I don't
know" to everything looks like a working feature), and a duplicate id across files must be an
error rather than last-one-wins (two teams overwriting each other's help is the exact failure the
one-file-per-area layout exists to prevent).
"""
from __future__ import annotations

import asyncio
import json

import pytest

from app.adapters.knowledge_source.file import FileKnowledgeSource
from app.core.kb import KbError
from app.ports.errors import ConfigError


def run(c):
    return asyncio.run(c)


def _write(p, doc):
    p.write_text(json.dumps(doc, ensure_ascii=False), encoding="utf-8")
    return p


def test_it_loads_a_single_file(tmp_path):
    f = _write(tmp_path / "kb.json", {"a.b": {"title": "T", "body": "B"}})
    snap = run(FileKnowledgeSource(str(f)).load())
    assert len(snap) == 1 and snap.entries[0].id == "a.b"
    assert snap.entries[0].source == "file"


def test_a_directory_merges_every_json_file(tmp_path):
    # One file per area is what makes parallel authoring practical without merge conflicts.
    _write(tmp_path / "billing.json", {"billing.x": {"title": "T", "body": "B"}})
    _write(tmp_path / "cameras.json", {"cameras.y": {"title": "T", "body": "B"}})
    snap = run(FileKnowledgeSource(str(tmp_path)).load())
    assert {e.id for e in snap.entries} == {"billing.x", "cameras.y"}


def test_a_duplicate_id_across_files_is_refused(tmp_path):
    _write(tmp_path / "a.json", {"same.id": {"title": "A", "body": "B"}})
    _write(tmp_path / "b.json", {"same.id": {"title": "C", "body": "D"}})
    with pytest.raises(KbError, match="duplicate"):
        run(FileKnowledgeSource(str(tmp_path)).load())


def test_a_missing_kb_refuses_loudly_instead_of_serving_nothing(tmp_path):
    with pytest.raises(ConfigError, match="would answer nothing"):
        run(FileKnowledgeSource(str(tmp_path / "absent.json")).load())


def test_invalid_json_names_the_file(tmp_path):
    (tmp_path / "broken.json").write_text("{not json", encoding="utf-8")
    with pytest.raises(KbError, match="broken.json"):
        run(FileKnowledgeSource(str(tmp_path)).load())


def test_an_empty_path_is_a_config_error():
    with pytest.raises(ConfigError):
        FileKnowledgeSource("")


# ── generation: reload by content, not by clock ───────────────────────────────
def test_rewriting_identical_bytes_does_not_change_the_generation(tmp_path):
    doc = {"a.b": {"title": "T", "body": "B"}}
    f = _write(tmp_path / "kb.json", doc)
    first = run(FileKnowledgeSource(str(f)).load()).generation
    _write(tmp_path / "kb.json", doc)          # touched, same content
    assert run(FileKnowledgeSource(str(f)).load()).generation == first


def test_an_edit_changes_the_generation_even_if_the_clock_did_not(tmp_path):
    # An mtime check would miss this; a content hash cannot.
    f = _write(tmp_path / "kb.json", {"a.b": {"title": "T", "body": "B"}})
    first = run(FileKnowledgeSource(str(f)).load()).generation
    _write(tmp_path / "kb.json", {"a.b": {"title": "T", "body": "CHANGED"}})
    assert run(FileKnowledgeSource(str(f)).load()).generation != first


def test_health_is_false_when_the_kb_is_gone(tmp_path):
    f = _write(tmp_path / "kb.json", {"a.b": {"title": "T", "body": "B"}})
    src = FileKnowledgeSource(str(f))
    assert run(src.health()) is True
    f.unlink()
    assert run(src.health()) is False
