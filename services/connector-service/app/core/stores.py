"""Repositories over the generic DataStore + a per-tenant cache generation."""
from __future__ import annotations

import time as _time

from app.core.models import Connection, ProviderSpec, SyncRecord, SyncRun
from app.ports.datastore import DataStore

_CONN = "connections"
_SPECS = "provider_specs"
_RUNS = "sync_runs"
_DLQ = "sync_dlq"
_SEEN = "sync_seen"        # per-record idempotency: key -> 1


class ConnectionStore:
    def __init__(self, store: DataStore) -> None:
        self._store = store

    async def save(self, c: Connection) -> Connection:
        await self._store.put(_CONN, c.id, c.to_dict())
        return c

    async def mutate(self, connection_id: str, apply) -> Connection | None:
        """Atomically load-mutate-save the Connection aggregate under an optimistic CAS (connector-003).

        The Connection carries watermarks, pending, circuit_state, failure_count, sync_count — a blind
        put() (last-writer-wins) let two concurrent pulls on different resources each load the row and
        clobber the other's watermark / circuit update. `apply(conn)` mutates in place; a lost CAS
        re-reads the freshest row and re-applies, so concurrent runs compose instead of overwriting."""
        for _ in range(8):
            row = await self._store.get(_CONN, connection_id)
            if row is None:
                return None
            ver = int(row.get("version", 0))
            c = Connection.from_dict(row)
            apply(c)
            if await self._store.cas(_CONN, connection_id, ver, c.to_dict()):
                return c
        raise RuntimeError("connection write contention")

    async def get(self, connection_id: str) -> Connection | None:
        row = await self._store.get(_CONN, connection_id)
        return Connection.from_dict(row) if row else None

    async def list(self, tenant: str, limit: int = 100, after: str | None = None) -> list[dict]:
        rows, _ = await self._store.find(_CONN, {"tenant": tenant}, limit=limit, after=after,
                                         sort=("created", "desc"))
        return rows

    async def delete(self, connection_id: str) -> None:
        await self._store.delete(_CONN, connection_id)


class SpecStore:
    def __init__(self, store: DataStore) -> None:
        self._store = store

    async def save(self, s: ProviderSpec) -> ProviderSpec:
        await self._store.put(_SPECS, s.id, s.to_dict())
        return s

    async def get(self, spec_id: str) -> ProviderSpec | None:
        row = await self._store.get(_SPECS, spec_id)
        return ProviderSpec.from_dict(row) if row else None

    async def list(self, tenant: str, limit: int = 100, after: str | None = None) -> list[dict]:
        rows, _ = await self._store.find(_SPECS, {"tenant": tenant}, limit=limit, after=after,
                                         sort=("created", "desc"))
        return rows

    async def delete(self, spec_id: str) -> None:
        await self._store.delete(_SPECS, spec_id)


class RunStore:
    def __init__(self, store: DataStore) -> None:
        self._store = store

    async def save(self, r: SyncRun) -> SyncRun:
        await self._store.put(_RUNS, r.id, r.to_dict())
        return r

    async def get(self, run_id: str) -> SyncRun | None:
        row = await self._store.get(_RUNS, run_id)
        return SyncRun.from_dict(row) if row else None

    async def list(self, tenant: str, limit: int = 100, after: str | None = None) -> list[dict]:
        rows, _ = await self._store.find(_RUNS, {"tenant": tenant}, limit=limit, after=after,
                                         sort=("started_at", "desc"))
        return rows


class DlqStore:
    def __init__(self, store: DataStore) -> None:
        self._store = store

    async def add(self, rec: SyncRecord) -> SyncRecord:
        await self._store.put(_DLQ, rec.id, rec.to_dict())
        return rec

    async def list(self, tenant: str, limit: int = 100, after: str | None = None) -> list[dict]:
        rows, _ = await self._store.find(_DLQ, {"tenant": tenant}, limit=limit, after=after,
                                         sort=("created", "desc"))
        return rows

    async def remove(self, record_id: str) -> None:
        await self._store.delete(_DLQ, record_id)


class SeenStore:
    """Per-record idempotency — a retried batch must not write the same record twice."""

    def __init__(self, store: DataStore) -> None:
        self._store = store

    async def seen(self, key: str) -> bool:
        return await self._store.get(_SEEN, key) is not None

    async def reserve(self, key: str) -> bool:
        """Atomically claim `key`: True if WE reserved it (first-seen → process), False if it was
        already taken. Replaces the non-atomic seen()->mark() pair so two concurrent syncs cannot both
        process the same record (double-write). cas(expected=0) is an insert-if-absent. Each row is
        stamped with `ts` so the prune sweep can bound the collection's growth (connector-r2-04)."""
        return await self._store.cas(_SEEN, key, 0, {"k": key, "ts": _time.time()})

    async def release(self, key: str) -> None:
        """Undo a reservation whose work then FAILED (e.g. a push whose provider call errored), so a
        retry may process the record again — otherwise a failed send would be skipped forever."""
        await self._store.delete(_SEEN, key)

    async def prune(self, *, older_than_seconds: int, now: float | None = None,
                    cap: int = 100_000) -> int:
        """Delete idempotency rows older than `older_than_seconds`. The sync_seen collection grew one
        permanent row per record forever (connector-r2-04); a TTL sized to the provider's max replay
        window bounds it while keeping dedup within the window. sweeper: a bounded page per run."""
        cutoff = (now if now is not None else _time.time()) - max(0, int(older_than_seconds))
        removed = 0
        # sweeper: a retention pass over the idempotency ledger — a full read IS the operation.
        for row in await self._store.scan_all(_SEEN, cap):
            if float(row.get("ts", 0)) < cutoff:
                await self._store.delete(_SEEN, row.get("k") or row.get("id", ""))
                removed += 1
        return removed
