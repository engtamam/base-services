# Phase 23 — analytics-service (detailed steps)

> **Goal:** a **general, plug-and-play, smart** product/business/operational analytics service
> (SaaS checklist §15). Idempotent event ingest, **pre-aggregated rollups**, **mathematically
> correct** unique counts (HyperLogLog) and percentiles (mergeable histograms), funnels,
> retention cohorts, cardinality guards, late-event handling, UTC bucketing with tenant-timezone
> rendering, PII allowlist, CSV export, and a warehouse port.
>
> It is the natural consumer of the events the platform already emits: `flag.exposed`,
> `job.*`, `webhook.*`, `sync.*`, billing/payment events.
>
> _Planned **past** the checklist per memory `phase-doc-rules` — the four questions are answered in §0 before any code._
> _Rules: self-contained · zero-hardcoded · ports-and-adapters · production-ready · permissive · CWE-guard · KSA · backend-only. No v2 — one pass, full._

---

## 0. Definition of done

### The four questions (answered before the plan)

**(1) Hot path? → TWO, needing OPPOSITE treatments.**
- **Ingest** is write-heavy. Caching is the wrong lever (same as the job queue). Prove instead:
  - [ ] `ingest(event)` = **exactly 1 event write + N rollup updates, 0 scans**.
  - [ ] **Idempotent by `event_id`** — our EventBus is at-least-once, so a redelivered event must not double-count. A duplicate does **0 additional writes**.
- **Query** is read-heavy and cacheable.
  - [ ] Results cached by `(tenant, generation, query)`; the generation bumps when a rollup changes.
  - [ ] **Benchmark**: repeated query = **0 datastore reads** after warm; and a query over a 5 000-event store reads **rollup rows only — never the raw event table**.

**(2) Every config knob has a consumer → guard test from the start.**

**(3) What production needs that the checklist omits → build it.**
The checklist asks for "percentiles" and "retention metrics" without noting that the obvious implementations are **arithmetically wrong**:
- [ ] **Mergeable unique counts (HyperLogLog).** `sum(daily_uniques) ≠ monthly_uniques`. Uniques must be sketches you can union.
- [ ] **Mergeable histograms for percentiles.** `avg(hourly_p95) ≠ daily_p95`. Percentiles must come from merged histograms.
- [ ] **Idempotent ingest** — double-counted revenue is a wrong MRR.
- [ ] **Cardinality guard** — cap distinct values per dimension, spill the tail to `__other__`, so `group by url` can't explode storage.
- [ ] **Late / out-of-order events** — bucket by **event_time**, not ingest time; reject beyond `LATE_EVENT_MAX_SECONDS` (and count them, don't silently drop).
- [ ] **Bucket in UTC, render in the tenant's timezone** (a "daily" KSA report is UTC+3).
- [ ] **PII allowlist** on properties — analytics is where PII leaks.
- [ ] **Query cost limits** (max time range, max groups) + raw-event **retention/downsampling**.
- [ ] **Funnels** (ordered steps within a window) and **retention cohorts**.
- [ ] **CSV export** + a `Warehouse` port (null · csv) for ETL.

**(4) Correctness footguns.**
- [ ] **Double counting** → idempotent ingest keyed on `event_id`.
- [ ] **Summing uniques** → HLL union, tested against exact counts.
- [ ] **Averaging percentiles** → merged histograms, tested against the exact quantile.
- [ ] **Cardinality explosion** → dimension cap + `__other__`.
- [ ] **Late events in the wrong bucket** → event-time bucketing, bounded lateness.
- [ ] **Timezone/DST** → UTC buckets, tenant-tz rendering only at the edge.
- [ ] **Unbounded raw scans** → queries read rollups; a guard test asserts the raw table is untouched.

### Functional (checklist §15)
- [ ] Ingest (single + batch); metrics: count / sum / unique / histogram; group-by dimensions; time range + granularity; funnels; retention cohorts; CSV export; warehouse export; report definitions saved/listed.

### Gates
- [ ] `pytest` + `ci/security_gate.py` green; **ingest + query hot-path benchmarks**, **config-knob guard** pass; live boot proves each item. **One pass, no v2.**

---

## 1. Ports

| Port | Drivers | Role |
|------|---------|------|
| `Warehouse` | `null` · `csv` | export rollups/raw for ETL / BI |
| `Clock` | `system` · `manual` | bucket + lateness math, injected for tests |

Reuses `DataStore` (events + rollups), `Cache` (query hot path), `EventBus` (subscribe to platform events), `Telemetry`.

## 2. Steps

| Task | Step |
|---|---|
| 23.1 | Skeleton |
| 23.2 | Config (every knob consumed) + `.env.example` |
| 23.3 | `core/hll.py` (mergeable unique sketch) + `core/histogram.py` (mergeable percentiles) — pure, tested against exact |
| 23.4 | `core/buckets.py` (UTC granularity + tenant-tz render) + `core/query.py` (DSL + cost limits) — pure |
| 23.5 | Ports + adapters (warehouse null/csv; clock) + registry |
| 23.6 | Models + stores (Event, Rollup, ReportDef) + generation + seen-index |
| 23.7 | Core `IngestService` — idempotent, event-time bucketing, cardinality guard, PII allowlist, late-event counter |
| 23.8 | Core `QueryService` — cached, rollup-only, group-by, uniques via HLL, percentiles via histogram merge, funnels, retention cohorts, CSV export |
| 23.9 | Routes + manifest |
| 23.10 | Tests — ingest/query benchmarks, knob guard, HLL vs exact, percentile-merge vs exact, dedupe, cardinality, late events, tz, PII |
| 23.11 | Verify + live boot + docs + memory |

## 3. Out of scope
BigQuery/Snowflake drivers (drop-in behind the `Warehouse` port — their knobs ship *with* the adapter) · BI dashboards / report-builder UI · scheduled-report delivery (job-service cron + notification-service already do this; we expose the report definition + CSV).

---

**STATUS: ✅ ALL DONE — 189 tests, gate PASS, live verified. Guards + exactness tests caught 3 real defects: `quantile()` returned the bucket's UPPER edge (every percentile biased high by up to +25%; now interpolates within the bucket → ≤2.5% error at p99), `_index()` used `floor(log)+1` so exact powers landed one bucket high, and `query.validate()` accepted `granularity="minute"` which ingest never materialises (a silent `0` — now a 400). Also replaced pure-HLL with an HLL++-style **exact sparse mode** (≤2^p/4 distinct values per cell), because linear counting reported 494 of a small tenant's 500 daily actives — indistinguishable from a bug.**
