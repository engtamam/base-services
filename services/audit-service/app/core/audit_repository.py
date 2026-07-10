"""AuditRepository over the generic DataStore port. Append-only + per-partition head."""
from __future__ import annotations

from typing import Any

from app.ports.datastore import DataStore

_EVENTS = "audit_events"   # id -> event dict (with internal _partition, _seq_key)
_META = "audit_meta"       # partition -> {partition, seq, head_hash}

# How many events a single find() page pulls while walking the chain. The chain is walked to
# EXHAUSTION — this bounds memory, never the walk.
_PAGE = 500

# A sweeper bound for the tiny _META collection (one row per partition), never for events.
_META_MAX = 10_000

# `value->>'seq'` is TEXT in postgres, so ordering by it puts '10' before '9' and the chain is
# walked out of order. A zero-padded key sorts correctly as text in BOTH adapters.
_SEQ_WIDTH = 18


def seq_key(seq: int) -> str:
    return f"{int(seq):0{_SEQ_WIDTH}d}"


class AuditRepository:
    def __init__(self, store: DataStore) -> None:
        self._store = store

    async def head(self, partition: str) -> tuple[int, str]:
        m = await self._store.get(_META, partition)
        return (int(m["seq"]), m["head_hash"]) if m else (0, "")

    async def append(self, event: dict[str, Any], partition: str) -> None:
        stored = {**event, "_partition": partition, "_seq_key": seq_key(event["seq"])}
        await self._store.put(_EVENTS, event["id"], stored)
        await self._store.put(_META, partition,
                              {"partition": partition, "seq": event["seq"], "head_hash": event["hash"]})

    async def partitions(self) -> list[str]:
        """From the tiny _META collection — one row per partition — never from the events.

        This used to scan every event in the store to collect a handful of distinct strings.
        """
        rows = await self._store.scan_all(_META, _META_MAX)
        return sorted({r.get("partition", "global") for r in rows if r.get("partition")})

    async def iterate(self, partition: str) -> list[dict[str, Any]]:
        """The WHOLE chain, in seq order, paged to exhaustion.

        This backs verify(). It previously read `list(_EVENTS, 1_000_000)`: past that cap the
        adapter returned whatever the storage engine yielded first, and verify() walked an
        arbitrary subset — reporting a clean chain having checked part of it, or crying tamper
        on an intact one. Which of the two depended on the adapter's row order.

        There is no cap here. A tamper-evident log that verifies part of itself is worse than
        no log.
        """
        out: list[dict[str, Any]] = []
        cursor: str | None = None
        while True:
            rows, cursor = await self._store.find(
                _EVENTS, {"_partition": partition},
                limit=_PAGE, after=cursor, sort=("_seq_key", "asc"))
            out.extend(rows)
            if not cursor:
                return out

    async def get(self, event_id: str) -> dict[str, Any] | None:
        return await self._store.get(_EVENTS, event_id)

    async def replace(self, event: dict[str, Any]) -> None:
        """Overwrite a stored event IN PLACE — used only by retention purge /
        tests. Not exposed via the API (append-only contract)."""
        await self._store.put(_EVENTS, event["id"], event)

    async def delete(self, event_id: str) -> None:
        await self._store.delete(_EVENTS, event_id)

    async def set_head(self, partition: str, seq: int, head_hash: str) -> None:
        await self._store.put(_META, partition, {"seq": seq, "head_hash": head_hash})

    # Equality filters the datastore can push down. `since`/`until` are ranges, so they stay a
    # post-filter — but they run over PAGES, never over the whole table.
    _PUSHDOWN = ("service", "actor", "action", "resource", "tenant")

    async def query(self, filters: dict[str, Any], limit: int,
                    partition: str = "global") -> list[dict[str, Any]]:
        """Newest first, bounded. The equality filters are pushed into the datastore."""
        where: dict[str, Any] = {"_partition": partition}
        for key in self._PUSHDOWN:
            if filters.get(key):
                where[key] = filters[key]

        out: list[dict[str, Any]] = []
        cursor: str | None = None
        # Bound the walk: a range filter that matches nothing must not read the whole table.
        for _ in range(max(1, (limit // _PAGE) + 2)):
            rows, cursor = await self._store.find(
                _EVENTS, where, limit=_PAGE, after=cursor, sort=("_seq_key", "desc"))
            for r in rows:
                if self._matches(r, filters):
                    out.append({k: v for k, v in r.items() if not k.startswith("_")})
                    if len(out) >= limit:
                        return out
            if not cursor:
                break
        return out

    @staticmethod
    def _matches(row: dict[str, Any], f: dict[str, Any]) -> bool:
        for key in ("service", "actor", "action", "resource", "tenant"):
            if f.get(key) and row.get(key) != f[key]:
                return False
        if f.get("since") and row.get("ts", "") < f["since"]:
            return False
        if f.get("until") and row.get("ts", "") > f["until"]:
            return False
        return True
