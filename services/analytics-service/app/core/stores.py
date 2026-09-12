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

# Events are the highest-volume collection in the platform. Only retention may read it whole.
_PAGE = 500
_EXPORT_MAX = 100_000     # a bounded export, not "everything that exists"
_PRUNE_MAX = 100_000      # one retention pass; the next tick takes the next batch
_ROLLUP_MAX = 100_000     # pre-aggregated buckets, not raw events


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

    async def reserve(self, event_id: str) -> bool:
        """Atomically claim event_id: True if first-seen (process it), False if already seen. Replaces
        the non-atomic seen()->mark_seen() pair so two concurrent redeliveries cannot both count."""
        return await self._store.cas(_SEEN, event_id, 0, {"id": event_id})

    async def for_tenant(self, tenant: str, limit: int = _EXPORT_MAX) -> list[dict]:
        """One tenant's raw events, oldest first - funnels / retention / export only.

        Never a metric query: those are served from the rollups. It used to read every tenant's
        events to answer for one, and past the cap it answered with an arbitrary subset - a
        funnel computed over a random sample of the wrong customers' events.
        """
        out: list[dict] = []
        cursor = None
        while len(out) < limit:
            rows, cursor = await self._store.find(_EVENTS, {"tenant": tenant}, limit=_PAGE,
                                                  after=cursor, sort=("event_time", "asc"))
            out.extend(rows)
            if not cursor:
                break
        return out[:limit]

    async def for_tenant_window(self, tenant: str, lo: float, hi: float,
                                limit: int = _EXPORT_MAX) -> tuple[list[dict], bool]:
        """Events in [lo, hi] for one tenant, oldest first, and whether the read was TRUNCATED at the
        cap. Reads only the window (events are sorted by event_time, so it stops once past hi) instead
        of the oldest N of ALL time — the old for_tenant() computed a funnel over the oldest 100k
        events regardless of the requested range, silently answering over the wrong subset. Returns
        truncated=True when the window itself exceeds the cap, so the caller can flag the result."""
        out: list[dict] = []
        cursor = None
        while True:
            rows, cursor = await self._store.find(_EVENTS, {"tenant": tenant}, limit=_PAGE,
                                                  after=cursor, sort=("event_time", "asc"))
            for r in rows:
                et = float(r.get("event_time", 0))
                if et < lo:
                    continue
                if et > hi:
                    return out, False            # sorted ascending → nothing more in window
                out.append(r)
                if len(out) >= limit:
                    return out, True             # window has more events than the cap
            if not cursor:
                break
        return out, False

    async def prune_before(self, cutoff: float) -> int:
        """Retention. Deletes every event older than the cutoff, for every tenant."""
        removed = 0
        # sweeper: retention runs on a timer and its job IS to visit every event. It is the one
        # place a full read of this collection is the operation rather than a mistake.
        for r in await self._store.scan_all(_EVENTS, _PRUNE_MAX):
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

    async def cas_put(self, r: Rollup, expected_version: int) -> bool:
        """Version-checked write for the concurrent read-modify-write path: True if this write won,
        False if another ingest updated the same cell first (the caller must re-read and retry, or
        the increment is lost — the undercount bug)."""
        return await self._store.cas(_ROLLUPS, r.key, expected_version, r.to_dict())

    async def get_many(self, keys: list[str]) -> list[Rollup]:
        """The query hot path: keyed reads of exactly the buckets in range."""
        out: list[Rollup] = []
        for k in keys:
            row = await self._store.get(_ROLLUPS, k)
            if row:
                out.append(Rollup.from_dict(row))
        return out

    async def scan_prefix(self, prefix: str, limit: int = _ROLLUP_MAX) -> list[Rollup]:
        """Group-by needs every dim-combination in a bucket - still rollups, never raw events."""
        # sweeper: a PREFIX match is not equality, and find() deliberately speaks equality only.
        # Rollups are pre-aggregated (one row per bucket per dim-combination), which is exactly
        # why the query path reads them instead of the events.
        return [Rollup.from_dict(r) for r in await self._store.scan_all(_ROLLUPS, _ROLLUP_MAX)
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

    async def list(self, tenant: str, limit: int = 100, after: str | None = None) -> list[dict]:
        rows, _ = await self._store.find(_REPORTS, {"tenant": tenant}, limit=limit, after=after,
                                         sort=("created", "desc"))
        return rows

    async def delete(self, report_id: str) -> None:
        await self._store.delete(_REPORTS, report_id)
