# job-service

> Self-contained, **general & plug-and-play** background-job + scheduler service (SaaS
> checklist §12 Async Processing): durable enqueue, **atomic lease-based claim**, retry
> with jittered backoff, dead-letter, **cron/scheduled jobs**, unique/singleton jobs,
> priority + per-tenant fairness, and monitoring. It is the **scheduler** the other
> services defer to — dunning, webhook-retry, audit-retention, billing-cycle, zatca sweeps.
> Backend only. **Zero new runtime deps** (a pure UTC cron evaluator).

---

## Why a lease, not just a queue

The checklist says "workers + retry + DLQ". What makes a queue *correct* is what it omits:

- **Lease / visibility timeout** — a claimed job is invisible for `LEASE_SECONDS`. If the
  worker dies without acking, `reclaim()` returns it to the queue. **A crash never loses a
  job.** (Proven live: crash → reclaim → redelivered to another worker.)
- **Atomic claim** — a claim moves a job `queued → leased` and removes it from the
  claimable index, so **two workers never run the same job**.
- **Fencing token** — every `ack`/`nack`/`heartbeat` carries the lease token. A worker
  whose lease expired (and was reclaimed by someone else) **cannot mutate the job it no
  longer owns** — its late ack is a no-op.
- **Poison-pill DLQ** — each attempt (including a crash-reclaim) counts; at `max_attempts`
  the job dead-letters instead of looping forever. `replay` re-drives it.

At-least-once delivery ⇒ **handlers must be idempotent**; we hand them a stable `job_id`
and support `unique_key`/`idempotency_key` so a duplicate is easy to reject.

## Scheduler (the reason this exists)

`POST /internal/scheduler/tick` (a deployment cron or the worker loop calls it) enqueues a
run for every schedule whose time has come, then advances via the **5-field cron
expression, evaluated in UTC** (`core/cron.py`). UTC ⇒ no DST ambiguity. The evaluator
gets the subtle rules right: `*/n` steps, ranges, lists, and the **day-of-month OR
day-of-week** rule; an impossible date (`Feb 30`) fails closed instead of looping. A
**singleton** schedule skips a run while the previous one is still active (no stacking ten
nightly sweeps).

```
Every service's sweep becomes a cron schedule here:
  "0 * * * *"  billing.dunning        "*/10 * * * *"  webhook.retry
  "0 3 * * *"  audit.enforce-retention "*/5 * * * *"  zatca.sweep
```

## Ports

| Port | Drivers | Role |
|------|---------|------|
| `Clock` | `system` · `manual` | now() — injected so lease/cron timing is deterministic in tests |
| `CronParser` | `builtin` | next fire time (UTC) |

Reuses `DataStore` (jobs + due-index + leased-index + schedules), `EventBus` (`job.*`).

## Hot path

A queue is mutable state, so "cache to zero reads" doesn't apply. Instead:
- `enqueue` = **1 job write + 1 due-index write, 0 scans**.
- `claim` reads the small **due-index** (bounded by `batch ≤ CLAIM_BATCH_MAX`), **never the
  job table**; `reclaim` reads the **leased-index**, never the job table. (Benchmarks assert
  both against a 200-job store.) A SQL adapter serves these from an index on
  `(queue, next_run_at, priority)`.

## Fairness & priority

Claims are ordered by **priority** (desc), then **round-robin across tenants** within a
priority tier — one tenant's flood of same-priority jobs can't starve another's.

## Endpoints

| Tier | Method · Path | Purpose |
|------|---------------|---------|
| internal | `POST /internal/jobs/enqueue` | any service enqueues work (idempotent) |
| internal | `POST /internal/jobs/claim` · `/ack` · `/nack` · `/heartbeat` | worker loop |
| internal | `POST /internal/jobs/reclaim` | return crashed workers' jobs (visibility timeout) |
| internal | `POST /internal/scheduler/tick` | fire due cron schedules |
| user | `POST/GET /api/v1/jobs/schedules` · `DELETE /{id}` | manage schedules |
| user | `GET /api/v1/jobs` · `/dead-letters` · `/stats` | list / DLQ / monitoring |
| user | `POST /api/v1/jobs/{id}/replay` · `/cancel` | replay / cancel |

Caps: `platform.jobs` (cross-tenant) · `jobs.manage` (own tenant) · `jobs.read`.

## Run

```bash
py -3.12 -m venv .venv && .venv/Scripts/python -m pip install -e ".[dev,ci]"
uvicorn app.main:app
pytest -q ; python ci/security_gate.py
```

## Out of scope
Kafka/SQS broker adapters (a `Queue` broker port could back it later) · distributed
scheduler leader-election (single tick endpoint; a deploy runs it) · workflow/DAG
orchestration. The lease token is a worker secret (hidden from the public job view).
