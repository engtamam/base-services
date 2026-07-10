"""Repositories over the generic DataStore + a per-tenant cache generation."""
from __future__ import annotations

from app.core.models import Connection, ProviderSpec, SyncRecord, SyncRun
from app.ports.datastore import DataStore

_CONN = "connections"
_SPECS = "provider_specs"
_RUNS = "sync_runs"
_DLQ = "sync_dlq"
_SEEN = "sync_seen"        # per-record idempotency: key -> 1
_SCAN = 1_000_000


class ConnectionStore:
    def __init__(self, store: DataStore) -> None:
        self._store = store

    async def save(self, c: Connection) -> Connection:
        await self._store.put(_CONN, c.id, c.to_dict())
        return c

    async def get(self, connection_id: str) -> Connection | None:
        row = await self._store.get(_CONN, connection_id)
        return Connection.from_dict(row) if row else None

    async def list(self, tenant: str, limit: int = 100) -> list[dict]:
        rows = [r for r in await self._store.list(_CONN, _SCAN) if r.get("tenant") == tenant]
        return rows[:limit]

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

    async def list(self, tenant: str, limit: int = 100) -> list[dict]:
        return [r for r in await self._store.list(_SPECS, _SCAN) if r.get("tenant") == tenant][:limit]

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

    async def list(self, tenant: str, limit: int = 100) -> list[dict]:
        rows = [r for r in await self._store.list(_RUNS, _SCAN) if r.get("tenant") == tenant]
        rows.sort(key=lambda r: r.get("started_at", 0), reverse=True)
        return rows[:limit]


class DlqStore:
    def __init__(self, store: DataStore) -> None:
        self._store = store

    async def add(self, rec: SyncRecord) -> SyncRecord:
        await self._store.put(_DLQ, rec.id, rec.to_dict())
        return rec

    async def list(self, tenant: str, limit: int = 100) -> list[dict]:
        return [r for r in await self._store.list(_DLQ, _SCAN) if r.get("tenant") == tenant][:limit]

    async def remove(self, record_id: str) -> None:
        await self._store.delete(_DLQ, record_id)


class SeenStore:
    """Per-record idempotency — a retried batch must not write the same record twice."""

    def __init__(self, store: DataStore) -> None:
        self._store = store

    async def seen(self, key: str) -> bool:
        return await self._store.get(_SEEN, key) is not None

    async def mark(self, key: str) -> None:
        await self._store.put(_SEEN, key, {"k": key})
