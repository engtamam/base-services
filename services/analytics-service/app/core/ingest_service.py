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

import math
from typing import Any

from app.core import buckets as _buckets
from app.core import hll as _hll
from app.core import histogram as _hist
from app.core.models import OTHER, PII_SUSPECT, Event, Rollup, rollup_key
from app.core.pii import subject_ref
from app.core.stores import CardinalityStore, EventStore, RollupStore


class IngestError(ValueError):
    """A rejected event (non-finite value, out-of-range timestamp, missing id/name). Maps to 400 —
    the producer sent bad data; retrying the same bytes will not help."""

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
        # PII salt for pseudonymising `subject` before it is ever stored/hashed (empty only in
        # dev/test; production is boot-guarded). Composite-cardinality cap and the future-timestamp
        # skew allowance both fall back to existing knobs when not explicitly configured.
        self._salt = str(config.get("pii_salt", "") or "")
        self._max_combos = int(config.get("max_combinations",
                                          config.get("max_cardinality", 1000)) or 1000)
        self._future_skew = int(config.get("future_skew_seconds",
                                           config.get("late_max_seconds", 0)) or 0)

    @staticmethod
    def _validate(event_time: float | None, value: Any, now: float, future_skew: int) -> None:
        """Reject a poisoned event BEFORE it can touch a rollup. inf/nan `value` sums into a cell's
        `sum` and permanently makes every query over it nan/inf; a far-future `event_time` lands in
        a bucket no query range reaches yet consumes storage that retention never prunes."""
        try:
            v = float(value)
        except (TypeError, ValueError) as e:
            raise IngestError("VALUE_NOT_A_NUMBER") from e
        if not math.isfinite(v):
            raise IngestError("VALUE_NOT_FINITE")
        if event_time is not None:
            try:
                et = float(event_time)
            except (TypeError, ValueError) as e:
                raise IngestError("EVENT_TIME_NOT_A_NUMBER") from e
            if not math.isfinite(et) or et <= 0:
                raise IngestError("EVENT_TIME_INVALID")
            if et > now + max(0, future_skew):
                raise IngestError("EVENT_TIME_IN_FUTURE")

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
            dims[k] = v if ok else OTHER                  # spill the long-tail VALUE
        # Cap the COMBINATION too: k per-dim-capped properties still multiply into up to cap**k
        # distinct cells. If this tuple is a new combination past the cap, drop the dims entirely so
        # the event counts only into the no-dims total — storage stays bounded, the total stays exact.
        if dims:
            combo = ",".join(f"{k}={dims[k]}" for k in sorted(dims))
            if not await self._card.admit_combination(tenant, metric, combo, self._max_combos):
                return {}
        return dims

    async def ingest(self, *, event_id: str, tenant: str, name: str, event_time: float | None = None,
                     subject: str = "", value: float = 0.0, props: dict | None = None) -> dict:
        now = self._clock.now()
        # Validate BEFORE the reserve so a poisoned event never consumes an idempotency claim (which
        # would then suppress a corrected redelivery of the same id).
        self._validate(event_time, value, now, self._future_skew)

        # ATOMIC reserve (was seen()->mark_seen(), a check-then-set two concurrent redeliveries both
        # passed, double-counting). If we don't win the reserve, it is a duplicate — zero more writes.
        # Tenant-scoped so one tenant's event_id cannot suppress another's.
        if not await self._events.reserve(tenant, event_id):
            return {"accepted": True, "duplicate": True, "id": event_id}

        # Reserve-then-work is at-least-once-safe ONLY if a failed attempt frees its claim; otherwise
        # a mid-ingest failure turns at-least-once into at-most-once (the redelivery is refused, the
        # event lost). On any exception we release the reservation so the bus's retry reprocesses.
        try:
            et = float(event_time) if event_time else now
            late = _buckets.is_late(et, now, self._cfg["late_max_seconds"])

            # Pseudonymise the subject BEFORE it is stored or hashed: the raw subject may be an email
            # or phone, and the HLL sketch stores per-subject hashes in sparse mode — an UNSALTED
            # sha1(email) that survives erasure and is brute-forceable. A salted, tenant-scoped
            # subject_ref is stable (unique-counts and funnels/retention still group correctly) but
            # carries no recoverable identity.
            subj = subject_ref(self._salt, tenant, subject) if (self._salt and subject) else subject

            clean_props = self._filter_props(props or {})
            dims = await self._dims_for(tenant, name, clean_props)

            event = Event(id=event_id, tenant=tenant, name=name, event_time=et, ingest_time=now,
                          subject=subj, value=float(value), props=dims)
            await self._events.add(event)      # (tenant, event_id) already reserved above

            if late:
                # Counted, never silently dropped — and never merged into a historical bucket we
                # have already reported on.
                await self._update_rollups(tenant, f"{name}.late", et, subj, value, dims)
            else:
                await self._update_rollups(tenant, name, et, subj, value, dims)

            await self._bump(tenant)
        except Exception:
            await self._events.release(tenant, event_id)
            raise
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
                else:
                    # All retries lost the race. Do NOT fall through silently — that drops the
                    # increment (a permanent undercount / sum drift on the hottest cells). Raise so
                    # the caller/bus retries the whole idempotent ingest; the reservation is released
                    # by ingest()'s handler first, so the retry reprocesses instead of being deduped.
                    raise RuntimeError("ROLLUP_CAS_EXHAUSTED")

    async def ingest_batch(self, tenant: str, events: list[dict]) -> dict:
        # Validate EVERY event up front. Without this a malformed event mid-batch 500s the request
        # AFTER earlier events are already durably written and reserved — a partial, non-atomic
        # batch. Rejecting the whole batch (400) before any write keeps it all-or-nothing on bad
        # INPUT; the producer resends and idempotency dedupes anything already applied.
        now = self._clock.now()
        for i, e in enumerate(events):
            if not e.get("id") or not e.get("name"):
                raise IngestError(f"EVENT_MISSING_ID_OR_NAME:{i}")
            self._validate(e.get("event_time"), e.get("value", 0) or 0, now, self._future_skew)
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
