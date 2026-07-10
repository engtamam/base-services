"""Repositories over the generic DataStore.

`RollupStore` is what queries read. `EventStore` holds raw events for funnels/retention and
for warehouse export — a metric query must NEVER scan it.
"""
from __future__ import annotations

from app.core.models import Event, ReportDef, Rollup
from app.ports.datastore import DataStore

_EVENTS = "events"
_ROLLUPS = "rollups"
_SEEN = "event_seen"        # idempotency: event_id -> 1
_CARD = "dim_cardinality"   # "<tenant>|<metric>|<dim>" -> {values: [...]}
_REPORTS = "reports"
_SCAN = 1_000_000


class EventStore:
    def __init__(self, store: DataStore) -> None:
        self._store = store

    async def add(self, e: Event) -> Event:
        await self._store.put(_EVENTS, e.id, e.to_dict())
        return e

    async def seen(self, event_id: str) -> bool:
        return await self._store.get(_SEEN, event_id) is not None

    async def mark_seen(self, event_id: str) -> None:
        await self._store.put(_SEEN, event_id, {"id": event_id})

    async def for_tenant(self, tenant: str, limit: int = _SCAN) -> list[dict]:
        """Raw scan — funnels / retention / export only, never a metric query."""
        rows = [r for r in await self._store.list(_EVENTS, _SCAN) if r.get("tenant") == tenant]
        rows.sort(key=lambda r: r.get("event_time", 0))
        return rows[:limit]

    async def prune_before(self, cutoff: float) -> int:
        removed = 0
        for r in await self._store.list(_EVENTS, _SCAN):
            if float(r.get("event_time", 0)) < cutoff:
                await self._store.delete(_EVENTS, r["id"])
                removed += 1
        return removed


class RollupStore:
    def __init__(self, store: DataStore) -> None:
        self._store = store

    async def get(self, key: str) -> Rollup | None:
        row = await self._store.get(_ROLLUPS, key)
        return Rollup.from_dict(row) if row else None

    async def put(self, r: Rollup) -> Rollup:
        await self._store.put(_ROLLUPS, r.key, r.to_dict())
        return r

    async def get_many(self, keys: list[str]) -> list[Rollup]:
        """The query hot path: keyed reads of exactly the buckets in range."""
        out: list[Rollup] = []
        for k in keys:
            row = await self._store.get(_ROLLUPS, k)
            if row:
                out.append(Rollup.from_dict(row))
        return out

    async def scan_prefix(self, prefix: str, limit: int = _SCAN) -> list[Rollup]:
        """Group-by needs every dim-combination in a bucket — still rollups, never raw events."""
        return [Rollup.from_dict(r) for r in await self._store.list(_ROLLUPS, _SCAN)
                if str(r.get("key", "")).startswith(prefix)][:limit]


class CardinalityStore:
    """Caps distinct values per dimension so `group by url` cannot explode storage."""

    def __init__(self, store: DataStore) -> None:
        self._store = store

    @staticmethod
    def _key(tenant: str, metric: str, dim: str) -> str:
        return f"{tenant}|{metric}|{dim}"

    async def admit(self, tenant: str, metric: str, dim: str, value: str, cap: int) -> bool:
        """True if `value` may be stored as-is; False → the caller must use `__other__`."""
        key = self._key(tenant, metric, dim)
        row = await self._store.get(_CARD, key) or {"values": []}
        values = row["values"]
        if value in values:
            return True
        if len(values) >= cap:
            return False
        values.append(value)
        await self._store.put(_CARD, key, {"values": values})
        return True


class ReportStore:
    def __init__(self, store: DataStore) -> None:
        self._store = store

    async def save(self, r: ReportDef) -> ReportDef:
        await self._store.put(_REPORTS, r.id, r.to_dict())
        return r

    async def get(self, report_id: str) -> ReportDef | None:
        row = await self._store.get(_REPORTS, report_id)
        return ReportDef.from_dict(row) if row else None

    async def list(self, tenant: str, limit: int = 100) -> list[dict]:
        return [r for r in await self._store.list(_REPORTS, _SCAN) if r.get("tenant") == tenant][:limit]

    async def delete(self, report_id: str) -> None:
        await self._store.delete(_REPORTS, report_id)
