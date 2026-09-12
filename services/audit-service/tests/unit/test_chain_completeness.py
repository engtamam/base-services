"""The chain read must be TOTAL, or verify() must say so.

Reported by a user who sells this chain to auditors:

    "A tamper-evident log that silently verifies part of itself is worse than no log."

Before this, `iterate()` read `list(_EVENTS, 1_000_000)`. Past that cap the adapter returned
whatever the storage engine yielded first, so `verify()` walked an arbitrary subset. Which
failure you got depended on the adapter:

    memory   -> insertion order -> a PREFIX  -> reports ok=True having checked part of the chain
    postgres -> ORDER BY key    -> arbitrary -> cries "tampered" on an intact chain

Both silent, in opposite directions.
"""
from __future__ import annotations

import asyncio
import hashlib
import json

from app.adapters.datastore.memory import MemoryDataStore
from app.core.audit_repository import AuditRepository, seq_key


def run(c):
    return asyncio.run(c)


class Integrity:
    def seal(self, prev, e):
        payload = json.dumps({k: v for k, v in e.items() if k not in ("hash",) and not k.startswith("_")},
                             sort_keys=True)
        return hashlib.sha256((prev + payload).encode()).hexdigest()


def _svc(store):
    from app.core.audit_service import AuditService
    return AuditService(repo=AuditRepository(store), integrity=Integrity(), exporters={},
                        default_format="json", partition_mode="global", query_limit=100)


def _chain(store, n, partition="global"):
    repo = AuditRepository(store)
    integ = Integrity()
    prev = ""
    for seq in range(1, n + 1):
        e = {"id": f"evt_{seq:05d}", "seq": seq, "prev_hash": prev, "service": "s", "action": "a",
             "ts": f"2026-01-01T00:00:{seq % 60:02d}"}
        e["hash"] = integ.seal(prev, e)
        run(repo.append_event(e, partition))    # event row; head advanced once below
        prev = e["hash"]
    run(repo.set_head_versioned(partition, run(repo.head_versioned(partition))[2], n, prev))
    return repo


def test_the_whole_chain_is_walked_not_a_page_of_it():
    store = MemoryDataStore()
    _chain(store, 1200)                     # more than one _PAGE (500)
    out = run(_svc(store).verify())
    assert out["ok"] is True and out["checked"] == 1200


def test_the_chain_is_walked_in_sequence_order_not_lexicographic_order():
    """`value->>'seq'` is TEXT in postgres: '10' sorts before '9'. A zero-padded key fixes it."""
    store = MemoryDataStore()
    _chain(store, 15)
    events = run(AuditRepository(store).iterate("global"))
    assert [e["seq"] for e in events] == list(range(1, 16))
    assert seq_key(9) < seq_key(10)          # the property the ordering depends on


def test_a_truncated_read_can_no_longer_report_a_clean_chain():
    """The exact failure the reporter feared: verify() clean, having read part of the log."""
    store = MemoryDataStore()
    _chain(store, 50)

    # Simulate an adapter that silently returns only a prefix, as list(_, cap) used to.
    real_find = store.find

    async def truncating_find(collection, where, **kw):
        rows, cursor = await real_find(collection, where, **kw)
        if collection == "audit_events":
            return rows[:20], None           # a prefix, and it claims there is no next page
        return rows, cursor

    store.find = truncating_find
    out = run(_svc(store).verify())
    assert out["ok"] is False
    assert "head" in out["reason"], out["reason"]
    assert out["checked"] == 20              # it says how much it actually read


def test_an_empty_read_against_a_non_empty_head_is_refused():
    store = MemoryDataStore()
    _chain(store, 10)

    async def empty_find(collection, where, **kw):
        return [], None

    store.find = empty_find
    out = run(_svc(store).verify())
    assert out["ok"] is False and out["checked"] == 0


def test_tampering_is_still_caught():
    store = MemoryDataStore()
    _chain(store, 10)
    row = run(store.get("audit_events", "evt_00005"))
    row["action"] = "tampered"
    run(store.put("audit_events", "evt_00005", row))
    out = run(_svc(store).verify())
    assert out["ok"] is False and out["first_bad_seq"] == 5


def test_a_deleted_event_breaks_the_chain():
    store = MemoryDataStore()
    _chain(store, 10)
    run(store.delete("audit_events", "evt_00005"))
    out = run(_svc(store).verify())
    assert out["ok"] is False


def test_partitions_come_from_the_meta_collection_not_from_scanning_events():
    store = MemoryDataStore()
    _chain(store, 5, "t1")
    _chain(store, 5, "t2")

    scans: list[str] = []
    real_scan = store.scan_all

    async def counting_scan(collection, limit):
        scans.append(collection)
        return await real_scan(collection, limit)

    store.scan_all = counting_scan
    assert run(AuditRepository(store).partitions()) == ["t1", "t2"]
    assert scans == ["audit_meta"], f"partitions() scanned {scans}"


def test_the_request_path_never_scans_the_event_table():
    store = MemoryDataStore()
    _chain(store, 30)
    scans: list[str] = []
    real_scan = store.scan_all

    async def counting_scan(collection, limit):
        scans.append(collection)
        return await real_scan(collection, limit)

    store.scan_all = counting_scan
    run(_svc(store).query({"service": "s"}, 10))
    assert "audit_events" not in scans


def test_the_million_row_constant_is_gone():
    import pathlib
    import re

    for name in ("app/core/audit_repository.py", "app/core/audit_service.py"):
        src = pathlib.Path(name).read_text(encoding="utf-8")
        code = "\n".join(line.split("#", 1)[0] for line in src.splitlines())
        code = re.sub(r'"""(.|\n)*?"""', "", code)
        assert "1_000_000" not in code and "1000000" not in code, name
