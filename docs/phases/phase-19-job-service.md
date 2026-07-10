# Phase 19 — job-service (detailed steps)

> **Goal:** a **general, plug-and-play, smart** background-job + scheduler service (SaaS
> checklist §12 Async Processing): durable enqueue, atomic lease-based claim, retry with
> jittered backoff, dead-letter, **cron/scheduled jobs**, unique/singleton jobs, priority
> + per-tenant fairness, and monitoring. It is the **scheduler** every other service has
> been deferring to (dunning, webhook-retry, audit-retention, billing-cycle, zatca sweeps).
>
> _Planned **past** the checklist per memory `phase-doc-rules` — the four questions are answered in §0 before any code._
> _Rules: self-contained · zero-hardcoded · ports-and-adapters · production-ready · permissive · CWE-guard · KSA · backend-only. No v2 — one pass, full._

---

## 0. Definition of done

### The four questions (answered before the plan)

**(1) Hot path? → YES (enqueue + claim). Caching is the WRONG lever for a queue — prove the right invariants instead.**
A queue is mutable state, not cacheable reads, so "cache to zero reads" doesn't apply. The correct hot-path guarantees, proven by a benchmark against a 5k-job store:
- [ ] `enqueue` does **exactly 1 job write + 1 due-index write, 0 scans** (and 0 extra writes when an idempotency key repeats).
- [ ] `claim(worker, batch)` reads the **due-index** and leases **≤ batch** jobs — it never scans the whole job table, and the work is bounded by `batch`, not by table size.
- [ ] Two concurrent claims **never return the same job** (atomic compare-and-set on `queued → leased`).

**(2) Every config knob has a consumer → guard test from the start.**
- [ ] `test_every_settings_field_is_consumed_somewhere` ships in the first test file.

**(3) What production needs that the checklist omits → build it.**
The checklist says "workers + retry + DLQ + monitoring". Production also needs:
- [ ] **Lease / visibility-timeout** — a claimed job is invisible for `lease`; if the worker dies without ack, the lease expires and the job is **redelivered** (never silently lost). *(This is the single most important queue property the checklist omits.)*
- [ ] **Atomic claim** — no two workers run the same job.
- [ ] **Scheduled / cron jobs** — recurring jobs with a cron expression (UTC), the reason this service exists. On each fire it enqueues a run; a **singleton guard** stops overlap if the previous run is still going.
- [ ] **Idempotent enqueue** — a retried enqueue with the same key returns the existing job, not a duplicate.
- [ ] **Unique / singleton jobs** — `unique_key` refuses a second identical pending job (no stacking 10 dunning sweeps).
- [ ] **Priority + per-tenant fairness** — higher priority first; round-robin across tenants so one tenant's flood can't starve others.
- [ ] **Jittered backoff** on retry (no thundering herd) + **poison-pill DLQ** at max attempts.
- [ ] **Cancellation** + **lease renewal (heartbeat)** for long jobs.
- [ ] **Monitoring** — per-queue depth, per-status counts, DLQ size, oldest-pending age.

**(4) Correctness footguns.**
- [ ] **Double-execution** → atomic CAS claim + lease token; a stable `job_id` + `unique_key` so handlers can dedupe (at-least-once ⇒ idempotent handlers).
- [ ] **Lost job on crash** → lease expiry returns it to `queued` (bounded by `max_attempts`, then DLQ).
- [ ] **Cron clock/DST** → next-run computed in **UTC**; never local time.
- [ ] **Poison pill infinite retry** → DLQ, never re-loop forever.
- [ ] **Unbounded claim** → `CLAIM_BATCH_MAX` cap.
- [ ] **ack/nack on a lease you no longer hold** (lease expired + reclaimed) → ack is a no-op unless the lease token matches (fencing token).

### Functional (checklist §12)
- [ ] Durable enqueue; worker claim/ack/nack; retry+DLQ; replay from DLQ; job + queue status; scheduled jobs CRUD.

### Gates
- [ ] `pytest` + `ci/security_gate.py` green; **hot-path benchmark + config-knob guard** pass; live boot proves each item. **One pass, no v2.**

---

## 1. Ports & reuse

| Port | Drivers | Role |
|------|---------|------|
| `Clock` | `system` · `manual` (tests) | now() — cron/lease are time-driven; injected so tests are deterministic |
| `CronParser` | `builtin` (5-field, UTC) | next_run_after(expr, after) |

Reuses `DataStore` (durable jobs + due-index + schedules), `EventBus` (emit `job.*`), `Telemetry`. No new runtime deps (a pure builtin cron evaluator; `croniter` optional `[cron]` extra only if we want 6-field/DST niceties later).

## 2. Steps

**STATUS: ✅ ALL DONE — 162 tests, gate PASS, live verified (crash→reclaim→redeliver, fencing, cron, singleton, DLQ, fairness).**

| Task | Step |
|---|---|
| 19.1 | Skeleton |
| 19.2 | Config (every knob consumed) + `.env.example` |
| 19.3 | Ports (`Clock`, `CronParser`) + `core/cron.py` (UTC, pure) + `core/backoff.py` (jitter) |
| 19.4 | Adapters + registry (clock system/manual; cron builtin) |
| 19.5 | Models + stores (Job, Schedule) + **due-index** + idempotency/unique index |
| 19.6 | Core `JobService` — enqueue (idempotent/unique), **atomic claim (lease)**, ack/nack, retry, DLQ, replay, cancel, heartbeat |
| 19.7 | Core `Scheduler` — cron schedules, singleton guard, `tick()` enqueues due runs |
| 19.8 | Fairness + priority claim ordering; monitoring (queue depth / DLQ / status counts) |
| 19.9 | Routes + manifest (internal enqueue/claim/ack/nack/tick; admin schedules/jobs/DLQ/replay/monitoring) |
| 19.10 | Tests — hot-path benchmark, config-knob guard, atomic-claim, lease-expiry redelivery, cron UTC, DLQ, fairness, idempotent/unique enqueue, fencing token |
| 19.11 | Verify + live boot |
| 19.12 | Docs + memory |

## 3. Out of scope
Kafka/SQS broker adapters (a `Queue` broker port could back it later; the durable-store model ships first) · distributed cron leader-election (single scheduler tick endpoint; a deploy runs it) · workflow/DAG orchestration.
