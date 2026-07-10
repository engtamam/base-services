"""`find()` — the primitive, and the bug it removes.

Reported by a user storing audit evidence. `FileRepository.list()` read up to a million rows
across every tenant, filtered in Python, and returned a hundred. Three consequences, in
ascending order of seriousness:

  * one tenant's request costs the size of everyone's data;
  * other tenants' rows cross the process boundary for no reason;
  * past the cap the answer is silently, ARBITRARILY wrong — `LIMIT` with no `ORDER BY` returns
    whatever the storage engine yields first, so a tenant can see none of its own files and
    nothing errors.

The last one is why this is not a performance ticket.
"""
from __future__ import annotations

import asyncio

import pytest

from app.adapters.datastore.memory import MemoryDataStore
from app.adapters.datastore import cursor as _cursor
from app.core.file_repository import FileRepository
from app.core.models import FileObject
from app.ports.errors import ConfigError


def run(c):
    return asyncio.run(c)


class CountingStore(MemoryDataStore):
    """Counts the rows that actually cross the process boundary."""

    def __init__(self):
        super().__init__()
        self.rows_returned = 0
        self.scans = 0

    async def find(self, collection, where, **kw):
        rows, nxt = await super().find(collection, where, **kw)
        self.rows_returned += len(rows)
        return rows, nxt

    async def scan_all(self, collection, limit):
        self.scans += 1
        rows = await super().scan_all(collection, limit)
        self.rows_returned += len(rows)
        return rows


def _file(store_id: str, tenant: str, created: str) -> FileObject:
    return FileObject(id=store_id, tenant=tenant, owner="u", filename="f.bin",
                      content_type="application/octet-stream", size=1,
                      sha256="a" * 64, created=created)


async def _seed(repo, tenant: str, n: int, prefix: str = "") -> None:
    for i in range(n):
        await repo.create(_file(f"{prefix}{tenant}-{i:05d}", tenant, f"2026-01-{i % 28 + 1:02d}"))


# ── the cost of a page is the size of the page ──────────────────────────────
def test_listing_a_tenants_files_does_not_read_other_tenants_rows():
    store = CountingStore()
    repo = FileRepository(store)
    run(_seed(repo, "mine", 5))
    run(_seed(repo, "theirs", 500))

    store.rows_returned = 0
    rows, _ = run(repo.list("mine", limit=100))
    assert len(rows) == 5
    assert {r["tenant"] for r in rows} == {"mine"}
    assert store.rows_returned == 5, (
        f"{store.rows_returned} rows crossed the process boundary to return 5"
    )


def test_the_cost_is_flat_in_the_number_of_foreign_rows():
    """The whole point. Before this, listing 5 files cost 5 + everyone else's data."""
    def rows_read(foreign: int) -> int:
        store = CountingStore()
        repo = FileRepository(store)
        run(_seed(repo, "mine", 5))
        run(_seed(repo, "theirs", foreign))
        store.rows_returned = 0
        run(repo.list("mine", limit=100))
        return store.rows_returned

    assert rows_read(10) == rows_read(10_000) == 5


def test_listing_never_scans_the_collection():
    store = CountingStore()
    repo = FileRepository(store)
    run(_seed(repo, "mine", 3))
    store.scans = 0
    run(repo.list("mine"))
    assert store.scans == 0, "the request path called scan_all()"


def test_a_tenant_with_many_files_still_sees_its_own():
    """Past the old 1_000_000 cap a tenant could see an arbitrary subset. Now it pages."""
    store = MemoryDataStore()
    repo = FileRepository(store)
    run(_seed(repo, "mine", 250))
    seen: list[str] = []
    cursor = None
    for _ in range(10):
        rows, cursor = run(repo.list("mine", limit=100, after=cursor))
        seen.extend(r["id"] for r in rows)
        if not cursor:
            break
    assert len(seen) == 250 and len(set(seen)) == 250


# ── the filter is not a query language ──────────────────────────────────────
def test_find_requires_a_non_empty_filter():
    """`find(c, {})` would return every tenant's rows — a leak one typo away."""
    store = MemoryDataStore()
    with pytest.raises(ConfigError):
        run(store.find("files", {}))


def test_find_supports_equality_and_membership_only():
    store = MemoryDataStore()
    run(store.put("files", "a", {"id": "a", "tenant": "t1", "created": "1"}))
    run(store.put("files", "b", {"id": "b", "tenant": "t2", "created": "2"}))
    run(store.put("files", "c", {"id": "c", "tenant": "t3", "created": "3"}))

    rows, _ = run(store.find("files", {"tenant": "t1"}))
    assert [r["id"] for r in rows] == ["a"]

    rows, _ = run(store.find("files", {"tenant": ["t1", "t3"]}, sort=("created", "asc")))
    assert [r["id"] for r in rows] == ["a", "c"]

    # An "operator" is just a field name that does not exist. Nothing is evaluated.
    rows, _ = run(store.find("files", {"$ne": "t1"}))
    assert rows == []


def test_an_unknown_sort_direction_is_refused():
    store = MemoryDataStore()
    with pytest.raises(ConfigError):
        run(store.find("files", {"tenant": "t"}, sort=("created", "sideways")))


# ── keyset paging: stable where an offset is not ────────────────────────────
def test_pages_do_not_overlap_or_skip():
    store = MemoryDataStore()
    for i in range(10):
        run(store.put("files", f"f{i}", {"id": f"f{i}", "tenant": "t", "created": f"{i:02d}"}))

    seen, cursor = [], None
    while True:
        rows, cursor = run(store.find("files", {"tenant": "t"}, limit=3, after=cursor))
        seen.extend(r["id"] for r in rows)
        if not cursor:
            break
    assert len(seen) == 10 and len(set(seen)) == 10


def test_an_insert_between_pages_does_not_skip_a_row():
    """An OFFSET would skip one here. A keyset cursor cannot."""
    store = MemoryDataStore()
    for i in range(6):
        run(store.put("files", f"f{i}", {"id": f"f{i}", "tenant": "t", "created": f"{i:02d}"}))

    page1, cursor = run(store.find("files", {"tenant": "t"}, limit=3))   # newest 3: 05,04,03
    # A new file arrives at the TOP of the ordering while the client reads page 2.
    run(store.put("files", "new", {"id": "new", "tenant": "t", "created": "99"}))
    page2, _ = run(store.find("files", {"tenant": "t"}, limit=3, after=cursor))

    ids = [r["id"] for r in page1] + [r["id"] for r in page2]
    assert len(ids) == len(set(ids)), "a row was returned twice"
    assert {"f0", "f1", "f2"} <= set(ids), "a row was skipped"


def test_rows_sharing_a_sort_value_do_not_straddle_a_page_boundary():
    """Without the id tie-break the order is not total, and a page boundary drops a row."""
    store = MemoryDataStore()
    for i in range(6):
        run(store.put("files", f"f{i}", {"id": f"f{i}", "tenant": "t", "created": "same"}))

    seen, cursor = [], None
    while True:
        rows, cursor = run(store.find("files", {"tenant": "t"}, limit=2, after=cursor))
        seen.extend(r["id"] for r in rows)
        if not cursor:
            break
    assert sorted(seen) == [f"f{i}" for i in range(6)]


def test_no_next_cursor_on_the_last_page():
    store = MemoryDataStore()
    run(store.put("files", "a", {"id": "a", "tenant": "t", "created": "1"}))
    rows, cursor = run(store.find("files", {"tenant": "t"}, limit=10))
    assert len(rows) == 1 and cursor is None


def test_a_corrupt_cursor_restarts_rather_than_crashing():
    store = MemoryDataStore()
    run(store.put("files", "a", {"id": "a", "tenant": "t", "created": "1"}))
    rows, _ = run(store.find("files", {"tenant": "t"}, after="not-a-cursor"))
    assert len(rows) == 1


def test_the_cursor_is_opaque_and_round_trips_exactly():
    token = _cursor.encode("2026-01-01T00:00:00.123456", "file_x")
    assert "2026" not in token                      # opaque: the format stays ours
    assert _cursor.decode(token) == ("2026-01-01T00:00:00.123456", "file_x")


# ── the constant that started it ────────────────────────────────────────────
def test_the_million_row_scan_constant_is_gone():
    """Checks CODE, not prose: the comment explaining the old constant may name it."""
    import pathlib
    import re

    src = pathlib.Path("app/core/file_repository.py").read_text(encoding="utf-8")
    code = "\n".join(line.split("#", 1)[0] for line in src.splitlines())
    code = re.sub(r'"""(.|\n)*?"""', "", code)          # drop docstrings too
    assert "1_000_000" not in code and "1000000" not in code


def test_the_repository_never_calls_scan_all():
    import pathlib

    src = pathlib.Path("app/core/file_repository.py").read_text(encoding="utf-8")
    code = "\n".join(line.split("#", 1)[0] for line in src.splitlines())
    assert "scan_all" not in code and ".list(" not in code.replace("def list(", "")


def test_scan_all_is_deterministic_when_truncated():
    """It may truncate — it must not truncate ARBITRARILY. The old list() had no ORDER BY."""
    store = MemoryDataStore()
    for i in range(10):
        run(store.put("files", f"f{i:02d}", {"id": f"f{i:02d}", "tenant": "t"}))
    first = [r["id"] for r in run(store.scan_all("files", 3))]
    second = [r["id"] for r in run(store.scan_all("files", 3))]
    assert first == second
