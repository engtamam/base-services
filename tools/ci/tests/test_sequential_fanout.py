"""The sequential-fanout gate: it must catch a service fan-out that awaits-then-collects, and NOT
cry wolf on a local-store loop, an unrelated loop, or an intentionally-serial one.

Sample-based (not run against the repo) so it stays green while the real backlog is worked through.
"""
from __future__ import annotations

import sys
import pathlib

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))
import check_sequential_fanout as g  # noqa: E402


def _check(tmp_path, src: str) -> list[str]:
    p = tmp_path / "sample.py"
    p.write_text(src, encoding="utf-8")
    return g.check_file(p)


def test_flags_a_service_fanout_loop(tmp_path):
    src = (
        "async def f(targets, fetch):\n"
        "    out = []\n"
        "    for t in targets:\n"
        "        r = await fetch(t)\n"        # injected fetcher — a fan-out
        "        out.append(r)\n"
    )
    assert _check(tmp_path, src), "a fetch-then-append fan-out loop must be flagged"


def test_ignores_a_local_store_loop(tmp_path):
    src = (
        "async def f(self, rows):\n"
        "    out = []\n"
        "    for row in rows:\n"
        "        await self._store.put('c', row['id'], row)\n"   # local port op, not a fan-out
        "        out.append(row['id'])\n"
    )
    assert _check(tmp_path, src) == [], "a datastore loop must not be flagged"


def test_serial_justification_silences_it(tmp_path):
    src = (
        "async def f(targets, fetch):\n"
        "    out = []\n"
        "    # serial: each step depends on the previous one's result\n"
        "    for t in targets:\n"
        "        r = await fetch(t)\n"
        "        out.append(r)\n"
    )
    assert _check(tmp_path, src) == [], "a # serial: justification must silence the finding"


def test_flags_an_await_comprehension(tmp_path):
    src = (
        "async def f(items, fetch):\n"
        "    return [await fetch(i) for i in items]\n"      # serial per element
    )
    assert _check(tmp_path, src), "an await inside a comprehension must be flagged"


def test_ignores_a_loop_with_no_accumulation(tmp_path):
    src = (
        "async def f(targets, fetch):\n"
        "    for t in targets:\n"
        "        await fetch(t)\n"          # fire-and-forget, not a collect — out of this gate's scope
    )
    assert _check(tmp_path, src) == [], "a loop that does not collect results is not the fan-out shape"


def test_ignores_a_store_verb_comprehension(tmp_path):
    src = (
        "async def f(self, ids):\n"
        "    return [await self._store.get('c', i) for i in ids]\n"   # store verb → not a fan-out
    )
    assert _check(tmp_path, src) == [], "a store-verb comprehension is out of scope"
