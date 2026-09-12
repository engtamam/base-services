"""AuditRepository over the generic DataStore port. Append-only + per-partition head."""
from __future__ import annotations

from typing import Any

from app.adapters.datastore import cursor as _cursor
from app.ports.datastore import DataStore

_EVENTS = "audit_events"   # id -> event dict (with internal _partition, _seq_key)
_META = "audit_meta"       # partition -> {partition, seq, head_hash}
# idempotency_key -> {state: pending|done, id, seq, hash, ts}. Lets the ingest recognise a retried
# append (returning the original receipt) instead of duplicating it in the tamper-evident chain.
_IDEMPO = "audit_idempo"

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

    async def head_versioned(self, partition: str) -> tuple[int, str, int]:
        """Head plus the optimistic-lock version, for an atomic advance. A missing head is version 0
        so the very first append advances it with an insert-if-absent (cas expected_version=0)."""
        m = await self._store.get(_META, partition)
        if not m:
            return 0, "", 0
        return int(m["seq"]), m["head_hash"], int(m.get("version", 0))

    async def advance_head(self, partition: str, expected_version: int, seq: int,
                           head_hash: str) -> bool:
        """Atomically move the chain head from `expected_version` to the next version. Returns False
        if another writer advanced it first — the caller then refetches and re-seals against the new
        head, so two concurrent appends can never chain onto the same prev_hash."""
        return await self._store.cas(_META, partition, expected_version,
                                     {"partition": partition, "seq": seq, "head_hash": head_hash})

    async def append_event(self, event: dict[str, Any], partition: str,
                           idempo_key: str = "") -> None:
        """Write the event row ONLY. The head is advanced separately by an atomic advance_head(),
        which is what serialises the chain — so this never touches _META.

        `idempo_key` (when supplied) is stamped as an internal, NON-hashed `_idempo_key` routing
        field: canonical() drops every `_`-prefixed key, so this never changes the chain hash, and
        query() strips it from results. It lets a stale idempotency take-over discover that the
        crashed reserver already appended THIS event (append precedes commit) and adopt its receipt
        instead of appending a duplicate to the tamper-evident chain."""
        stored = {**event, "_partition": partition, "_seq_key": seq_key(event["seq"])}
        if idempo_key:
            stored["_idempo_key"] = idempo_key
        await self._store.put(_EVENTS, event["id"], stored)

    async def partitions(self) -> list[str]:
        """From the tiny _META collection — one row per partition — never from the events.

        This used to scan every event in the store to collect a handful of distinct strings.
        """
        # sweeper: `audit_meta` holds ONE row per partition (a handful), not one per event.
        # This is the only scan_all() left in this service, and it is why `iterate()` and
        # `query()` never touch the event table without a partition to scope them.
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
        # Retention purge re-anchors the head. BUMP the version (read-then-cas-loop) so any append
        # holding an older head version fails its cas and refetches — a purge and a concurrent append
        # can never both write the head from the same base. Carry `partition` in the row, exactly as
        # append_event does, so partitions() and the backup key template can rely on the row shape.
        for _ in range(100):
            _, _, ver = await self.head_versioned(partition)
            if await self._store.cas(_META, partition, ver,
                                     {"partition": partition, "seq": seq, "head_hash": head_hash}):
                return
        raise RuntimeError("could not set audit head under contention")

    async def set_head_versioned(self, partition: str, expected_version: int, seq: int,
                                 head_hash: str) -> bool:
        """Move the head from `expected_version` in a SINGLE compare-and-set — no read-cas retry loop.

        This is purge's serialization point against append. Both purge's re-anchor commit and a
        concurrent append's advance_head() are a cas on the SAME _META version, so exactly one wins:
        if an append lands while purge is computing, purge's cas fails and it retries against the new
        tail rather than clobbering the head and orphaning the just-appended event (audit-r2-01)."""
        return await self._store.cas(
            _META, partition, expected_version,
            {"partition": partition, "seq": seq, "head_hash": head_hash})

    # ── idempotency ────────────────────────────────────────────────────────────────────────────
    # The row carries its own `key` so idempo_prune (which reads VALUES via scan_all) can delete it.
    async def idempo_get(self, key: str) -> dict[str, Any] | None:
        return await self._store.get(_IDEMPO, key)

    async def idempo_reserve(self, key: str, ts: float) -> bool:
        """Atomically claim an idempotency key for an in-flight append. True iff we won the claim.
        A cas insert-if-absent seeds version 1, so a later take-over can pin that version."""
        return await self._store.cas(_IDEMPO, key, 0, {"key": key, "state": "pending", "ts": ts})

    async def idempo_take_over(self, key: str, expected_version: int, ts: float) -> bool:
        """Re-claim a STALE pending key whose reserver never committed (crashed mid-append).

        ATOMIC now: the old plain put let two retries that both judged the entry stale each take over
        and each append, defeating the idempotency guard. This cas pins the version the caller
        observed, so exactly one concurrent take-over wins; the loser is told to retry."""
        return await self._store.cas(_IDEMPO, key, expected_version,
                                     {"key": key, "state": "pending", "ts": ts})

    async def idempo_find_event(self, idempo_key: str) -> dict[str, Any] | None:
        """The event already appended under this idempotency key, if any.

        A crash can land between record()'s append and idempo_commit(), leaving the key `pending`
        with the event already in the chain. Before a stale take-over re-runs record() (which would
        append a DUPLICATE), it asks this: an event stamped with `_idempo_key` means the reserver
        DID append, so its receipt is adopted rather than a second event chained (audit-r2-03)."""
        if not idempo_key:
            return None
        rows, _ = await self._store.find(
            _EVENTS, {"_idempo_key": idempo_key}, limit=1, sort=("_seq_key", "desc"))
        return rows[0] if rows else None

    async def idempo_commit(self, key: str, receipt: dict[str, Any], ts: float) -> None:
        await self._store.put(_IDEMPO, key, {"key": key, "state": "done", "ts": ts, **receipt})

    async def idempo_prune(self, before_ts: float, limit: int) -> int:
        """Delete idempotency keys older than `before_ts`. The retries these guard happen in seconds,
        so an entry past the window is only housekeeping — the receipt lives in the chain itself."""
        removed = 0
        # sweeper: pruning expired idempotency keys IS visiting the collection — a time-window
        # question the store's equality filter cannot express. Never a request path: this runs from
        # the retention/housekeeping cron, bounded by `limit` per pass.
        for row in await self._store.scan_all(_IDEMPO, limit):
            if float(row.get("ts", 0)) < before_ts and row.get("key"):
                if await self._store.delete(_IDEMPO, row["key"]):
                    removed += 1
        return removed

    # Equality filters the datastore can push down. `since`/`until` are ranges, so they stay a
    # post-filter — but they run over PAGES, never over the whole table.
    _PUSHDOWN = ("service", "actor", "action", "resource", "tenant")

    async def query(self, filters: dict[str, Any], limit: int, partition: str = "global",
                    after: str | None = None) -> tuple[list[dict[str, Any]], str | None]:
        """Newest first, bounded, and PAGEABLE. Returns (events, next_cursor); next_cursor is None
        when the walk reached the end, or an opaque keyset cursor to pass back as `after` when more
        events remain (so a caller who needs full history — a dispute report — can page past the
        per-request limit instead of being hard-capped). The equality filters are pushed down; the
        cursor is keyset over (_seq_key, id), so it never skips or duplicates as the log grows."""
        where: dict[str, Any] = {"_partition": partition}
        for key in self._PUSHDOWN:
            if filters.get(key):
                where[key] = filters[key]

        out: list[dict[str, Any]] = []
        cursor: str | None = after
        # Bound the walk: a range filter that matches nothing must not read the whole table.
        for _ in range(max(1, (limit // _PAGE) + 2)):
            rows, cursor = await self._store.find(
                _EVENTS, where, limit=_PAGE, after=cursor, sort=("_seq_key", "desc"))
            for r in rows:
                if self._matches(r, filters):
                    out.append({k: v for k, v in r.items() if not k.startswith("_")})
                    if len(out) >= limit:
                        # Resume AFTER this exact row on the next page — not the store's page cursor,
                        # which points past rows we did not return.
                        return out, _cursor.encode(r.get("_seq_key"), str(r.get("id", "")))
            if not cursor:
                break
        # Loop ended: a non-None store cursor means the bounded walk stopped with rows still unread
        # (a sparse range filter), so hand it back so the caller can continue; None means truly done.
        return out, cursor

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
