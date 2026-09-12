"""Event ingest — the write hot path.

Guarantees:
  • **Idempotent by `event_id`.** Our EventBus is at-least-once (`flag.exposed`, `job.*`,
    `webhook.*`, payment events), so a redelivery must not double-count. A duplicate performs
    zero additional writes. Double-counted revenue is a wrong MRR.
  • **Bucketed by `event_time`, not ingest time.** A delayed batch belongs in the hours it
    describes, not in "now". Events later than the lateness watermark are counted separately
    rather than silently rewriting old buckets forever.
  • **Cardinality-guarded.** A dimension that exceeds its cap spills to `__other__`, so a
    `group by url` cannot fill the disk.
  • **PII-filtered.** Known-PII property keys are dropped unless explicitly allow-listed.
  • **Pre-aggregated at write time** into mergeable rollups (count / sum / HLL / histogram) —
    which is what allows queries to never touch the raw event table.
"""
from __future__ import annotations

from typing import Any

from app.core import buckets as _buckets
from app.core import hll as _hll
from app.core import histogram as _hist
from app.core.models import OTHER, PII_SUSPECT, Event, Rollup, rollup_key
from app.core.stores import CardinalityStore, EventStore, RollupStore

# We roll every event up at these granularities so any query range is answerable from rollups.
# Single source of truth: `query.validate` refuses any granularity not in this same tuple.
from app.core.buckets import ROLLUP_GRANULARITIES as GRANULARITIES


class IngestService:
    def __init__(self, *, events: EventStore, rollups: RollupStore, cardinality: CardinalityStore,
                 clock, config: dict[str, Any], cache=None, event_bus=None) -> None:
        self._events = events
        self._rollups = rollups
        self._card = cardinality
        self._clock = clock
        self._cfg = config
        self._cache = cache
        self._bus = event_bus
        # Bounded retries for the rollup compare-and-set under contention; a loser re-reads and
        # re-applies its increment so nothing is lost.
        self._cas_retries = int(config.get("cas_max_retries", 8) or 8)

    async def _bump(self, tenant: str) -> None:
        if self._cache is None:
            return
        try:
            g = await self._cache.get(f"analyticsgen:{tenant}")
            await self._cache.set(f"analyticsgen:{tenant}", str(int(g or 0) + 1), None)
        except Exception:
            pass

    def _filter_props(self, props: dict) -> dict[str, str]:
        allowed = self._cfg["pii_allowed"]
        out: dict[str, str] = {}
        for k, v in (props or {}).items():
            key = str(k).lower()
            if key in PII_SUSPECT and key not in allowed:
                continue                                  # analytics is where PII leaks
            out[str(k)] = str(v)
        return out

    async def _dims_for(self, tenant: str, metric: str, props: dict[str, str]) -> dict[str, str]:
        cap = self._cfg["max_cardinality"]
        dims: dict[str, str] = {}
        for k, v in props.items():
            ok = await self._card.admit(tenant, metric, k, v, cap)
            dims[k] = v if ok else OTHER                  # spill the long tail
        return dims

    async def ingest(self, *, event_id: str, tenant: str, name: str, event_time: float | None = None,
                     subject: str = "", value: float = 0.0, props: dict | None = None) -> dict:
        now = self._clock.now()
        # ATOMIC reserve (was seen()->mark_seen(), a check-then-set two concurrent redeliveries both
        # passed, double-counting). If we don't win the reserve, it is a duplicate — zero more writes.
        if not await self._events.reserve(event_id):
            return {"accepted": True, "duplicate": True, "id": event_id}

        et = float(event_time) if event_time else now
        late = _buckets.is_late(et, now, self._cfg["late_max_seconds"])

        clean_props = self._filter_props(props or {})
        dims = await self._dims_for(tenant, name, clean_props)

        event = Event(id=event_id, tenant=tenant, name=name, event_time=et, ingest_time=now,
                      subject=subject, value=float(value), props=dims)
        await self._events.add(event)          # event_id already reserved above (atomic dedupe)

        if late:
            # Counted, never silently dropped — and never merged into a historical bucket we
            # have already reported on.
            await self._update_rollups(tenant, f"{name}.late", et, subject, value, dims)
        else:
            await self._update_rollups(tenant, name, et, subject, value, dims)

        await self._bump(tenant)
        return {"accepted": True, "duplicate": False, "id": event_id, "late": late,
                "dimensions": dims}

    async def _update_rollups(self, tenant: str, metric: str, event_time: float, subject: str,
                              value: float, dims: dict[str, str]) -> None:
        # MONEY CONTRACT: this is a generic numeric aggregator; `value` may legitimately be fractional
        # (a rate, a duration), so it stays a float. Per the platform money rule, a MONETARY metric
        # must be sent as an INTEGER in minor units (halalas) — and float summation of integers is
        # EXACT up to 2**53, so revenue rollups carry no rounding drift when that rule is followed. The
        # fix for audit-06 is this contract, not a type change that would break fractional metrics.
        # Roll up both the total (no dims) and the dimensioned cell, at each granularity.
        dim_sets = [{}] + ([dims] if dims else [])
        for granularity in GRANULARITIES:
            bucket = _buckets.bucket_key(event_time, granularity)
            for dset in dim_sets:
                key = rollup_key(tenant, metric, granularity, bucket, dset)
                # CAS-retry: read the cell, apply the increment/merges, write only if the version is
                # unchanged. Without this the read-modify-write raced — two ingests on the same cell
                # both read the base and the second overwrote the first, silently losing increments
                # (undercount). On a lost race we re-read and re-apply, so no increment is dropped.
                for _attempt in range(self._cas_retries):
                    r = await self._rollups.get(key) or Rollup(
                        key=key, tenant=tenant, metric=metric, granularity=granularity,
                        bucket=bucket, dims=dset,
                        hll=_hll.new(self._cfg["hll_precision"]),
                        hist=_hist.new(self._cfg["hist_buckets"]))
                    expected = int(r.version)
                    r.count += 1
                    r.sum += float(value)
                    if subject:
                        r.hll = _hll.add(r.hll or _hll.new(self._cfg["hll_precision"]), subject)
                    if value:
                        r.hist = _hist.add(r.hist or _hist.new(self._cfg["hist_buckets"]), float(value))
                    if await self._rollups.cas_put(r, expected):
                        break

    async def ingest_batch(self, tenant: str, events: list[dict]) -> dict:
        accepted = duplicates = 0
        for e in events:
            out = await self.ingest(event_id=e["id"], tenant=tenant, name=e["name"],
                                    event_time=e.get("event_time"), subject=e.get("subject", ""),
                                    value=float(e.get("value", 0) or 0), props=e.get("props"))
            duplicates += 1 if out["duplicate"] else 0
            accepted += 0 if out["duplicate"] else 1
        return {"accepted": accepted, "duplicates": duplicates}

    async def prune_raw(self) -> dict:
        """Raw events age out (rollups persist) — ties into the platform retention policy."""
        days = self._cfg["raw_retention_days"]
        if days <= 0:
            return {"pruned": 0, "enforced": False}
        cutoff = self._clock.now() - days * 86400
        return {"pruned": await self._events.prune_before(cutoff), "enforced": True}
