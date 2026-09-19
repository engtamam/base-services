"""Repositories over the generic DataStore.

`RollupStore` is what queries read. `EventStore` holds raw events for funnels/retention and
for warehouse export — a metric query must NEVER scan it.
"""
from __future__ import annotations

from app.core.models import Event, ReportDef, Rollup
from app.ports.datastore import DataStore

_EVENTS = "events"
_ROLLUPS = "rollups"
_SEEN = "event_seen"        # idempotency: "<len(tenant)>:<tenant>|<event_id>" -> 1
_CARD = "dim_cardinality"   # "<tenant>|<metric>|<dim>" -> {values: [...]}
_REPORTS = "reports"

# Highest code point — the exclusive upper bound of a key range, so a range read includes every
# dimension cell of the LAST bucket (whose keys are "...|<bucket>|<dims>", all < this sentinel).
_KEY_MAX = "￿"


def seen_key(tenant: str, event_id: str) -> str:
    """Tenant-scoped idempotency key. The dedupe record used to be keyed by the raw, caller-supplied
    event_id ALONE, so tenant B ingesting an id tenant A had already used was silently dropped as a
    duplicate — a cross-tenant undercount. The tenant is LENGTH-PREFIXED so no (tenant, id) pair can
    be forged to collide with another by moving the delimiter."""
    return f"{len(tenant)}:{tenant}|{event_id}"

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

    async def seen(self, tenant: str, event_id: str) -> bool:
        return await self._store.get(_SEEN, seen_key(tenant, event_id)) is not None

    async def reserve(self, tenant: str, event_id: str) -> bool:
        """Atomically claim (tenant, event_id): True if first-seen (process it), False if already
        seen. Replaces the non-atomic seen()->mark_seen() pair so two concurrent redeliveries cannot
        both count, and is tenant-scoped so one tenant's id cannot suppress another's (see seen_key)."""
        k = seen_key(tenant, event_id)
        return await self._store.cas(_SEEN, k, 0, {"id": k, "tenant": tenant, "event_id": event_id})

    async def release(self, tenant: str, event_id: str) -> None:
        """Undo a reservation whose processing FAILED. Reserve-then-work makes at-least-once safe
        only if a failed attempt frees its claim; otherwise a crash between the claim and the counted
        work turns at-least-once into at-most-once — the redelivery is refused and the event is lost
        forever. On any exception mid-ingest we release so the bus's retry reprocesses it."""
        await self._store.delete(_SEEN, seen_key(tenant, event_id))

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
        # cas-create: ok — the reachable create path for a rollup cell is IngestService._update_rollups,
        # which reads the cell and writes it back through cas_put(r, expected) with expected=0 on the
        # row-absent branch (an insert-if-absent create that stamps version=1). This put() is not on
        # that path — it is not called anywhere in app code — so no versionless row ever reaches the
        # version-pinned cas_put(); the postgres/memory divergence cannot arise here.
        await self._store.put(_ROLLUPS, r.key, r.to_dict())
        return r

    async def cas_put(self, r: Rollup, expected_version: int) -> bool:
        """Version-checked write for the concurrent read-modify-write path: True if this write won,
        False if another ingest updated the same cell first (the caller must re-read and retry, or
        the increment is lost — the undercount bug)."""
        return await self._store.cas(_ROLLUPS, r.key, expected_version, r.to_dict())

    async def get_many(self, keys: list[str]) -> list[Rollup]:
        """The query hot path: keyed reads of exactly the buckets in range, in ONE round trip.

        Was a per-key `get()` loop — up to ~9600 serial point reads for a 400-day hourly query,
        each pinning a pool slot. `store.get_many` issues a single `key = ANY(...)` batch."""
        if not keys:
            return []
        return [Rollup.from_dict(r) for r in await self._store.get_many(_ROLLUPS, keys)]

    async def range_cells(self, tenant: str, metric: str, granularity: str,
                          first_bucket: str, last_bucket: str,
                          limit: int = _ROLLUP_MAX) -> list[Rollup]:
        """Every dimension cell in a bucket RANGE, via ONE indexed key-range read — the group-by hot
        path. Replaces a `scan_all(_ROLLUPS, 100_000) + startswith` that (a) read the WHOLE rollup
        collection once PER bucket and (b) truncated at 100k rows ordered by key, so a high-volume
        early-sorting tenant could evict every later tenant's rollups from the window entirely (a
        cross-tenant correctness bug). A tenant-pinned range read is bounded by that tenant's own
        cells and cannot be starved by another's volume.

        Keys are `tenant|metric|granularity|bucket|dims`; buckets are fixed-width and time-sortable
        within a granularity, so [first, last] is a contiguous key range. The '|' after the bucket
        and the tenant length-independent prefix keep a metric/tenant whose name is a prefix of
        another (e.g. `checkout` vs `checkoutX`) from leaking in — '|' (0x7C) terminates the field."""
        if not first_bucket or not last_bucket:
            return []
        lo = f"{tenant}|{metric}|{granularity}|{first_bucket}|"
        hi = f"{tenant}|{metric}|{granularity}|{last_bucket}|{_KEY_MAX}"
        return [Rollup.from_dict(r) for r in await self._store.range_scan(_ROLLUPS, lo, hi, limit)]


class CardinalityStore:
    """Caps distinct values per dimension so `group by url` cannot explode storage."""

    def __init__(self, store: DataStore) -> None:
        self._store = store

    _CAS_RETRIES = 5

    @staticmethod
    def _key(tenant: str, metric: str, dim: str) -> str:
        return f"{tenant}|{metric}|{dim}"

    async def _admit_member(self, key: str, value: str, cap: int) -> bool:
        """True if `value` is (now) an admitted member of the set at `key`; False if the set is full.

        CAS read-modify-write: two concurrent admits of DIFFERENT new values used to both read the
        list and the second `put` dropped the first (a lost update), so a value looked un-admitted,
        got re-admitted next event, and slipped the cap. On a lost CAS we re-read and retry; if the
        set is genuinely full we spill (False). Retry exhaustion also spills — never a hard failure,
        since the cap is a storage bound, not a correctness fact."""
        for _ in range(self._CAS_RETRIES):
            row = await self._store.get(_CARD, key)
            values = list(row["values"]) if row else []
            if value in values:
                return True
            if len(values) >= cap:
                return False
            values.append(value)
            expected = int(row.get("version", 0)) if row else 0
            if await self._store.cas(_CARD, key, expected, {"id": key, "values": values}):
                return True
        return False

    async def admit(self, tenant: str, metric: str, dim: str, value: str, cap: int) -> bool:
        """True if `value` may be stored as-is; False → the caller must use `__other__`."""
        return await self._admit_member(self._key(tenant, metric, dim), value, cap)

    async def admit_combination(self, tenant: str, metric: str, combo: str, cap: int) -> bool:
        """Cap the number of distinct DIMENSION COMBINATIONS per (tenant, metric), not just each
        dimension independently. Per-dimension caps let k properties each within their cap multiply
        into up to cap**k distinct rollup rows (a storage-DoS the per-dim cap does not see); this
        bounds the composite so the excess spills to the no-dims total instead of exploding disk."""
        return await self._admit_member(self._key(tenant, metric, "__combo__"), combo, cap)


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
