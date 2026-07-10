# Phase 28 — `find()`: the missing DataStore primitive

> Reported by a user storing audit evidence: *"A tenant that cannot see its own files during an
> audit is not a performance problem for us. It is the product failing."*
>
> They are right, their diagnosis is right, and the problem is larger than the service they
> found it in.

---

## 0. The gap (verified, with lines)

| # | Finding | Evidence |
| --- | --- | --- |
| 1 | `FileRepository.list()` pulls up to **1 000 000 rows across all tenants** into Python, filters, sorts, returns 100. | `file-service/app/core/file_repository.py:24-27`, `_SCAN = 1_000_000` at `:9` |
| 2 | The port offers only `get` / `put` / `delete` / `list(collection, limit)`. A tenant filter **can only** be written in Python. | `file-service/app/ports/datastore.py:11-17` |
| 3 | The Postgres adapter issues `SELECT value FROM kv_store WHERE collection=$1 LIMIT $2` — **no `ORDER BY`**. Beyond the cap the surviving rows are *arbitrary*, not merely truncated. | `file-service/app/adapters/datastore/postgres.py:64-71` |
| 4 | **Twelve of seventeen services** scan-then-filter: analytics, audit, authz, billing, compliance, connector, feature-flag, file, job, wallet, webhook, zatca. **57 call sites.** | `grep -rn _SCAN services/*/app/core/` |
| 5 | **`LedgerStore.record()` scans the whole ledger on every WRITE** to compute a running balance, then stores it in an immutable row. | `billing-service/app/core/stores.py:177-185` |
| 6 | `UsageStore.aggregate()` scans every usage row to total one subscription's metric. | `billing-service/app/core/stores.py:213-219` |

(5) is not in the report and changes the severity. Past the cap, `record()` does not compute a
*slow* balance. It computes a **wrong** one, silently, from an arbitrary subset — and writes it
down as the reconciliation source of truth. The same primitive that hides a tenant's files
mis-states their money.

---

## 1. Definition of Done — answered BEFORE any code

### (1) Hot path

Both directions, and **caching is the wrong lever for both.** A cached list goes stale the
moment a file is uploaded, and you cannot cache your way out of O(n). The fix is an index and a
materialized balance.

* **Read:** `find({"tenant": t}, limit=100)` must cost the same whether other tenants hold 10
  rows or 100 000.
  * **Benchmark:** an instrumented store counts **rows crossing the process boundary**. Listing
    a tenant's 10 files returns ≤ `limit` rows, and the count is **identical** at 10 and at
    100 000 foreign rows. Today it is 100 010.
* **Write:** `LedgerStore.record()` must be **O(1)** in the length of the tenant's history.
  * **Benchmark:** the 1st and the 10 000th ledger write read the same number of rows.

### (2) Every config knob has a consumer

No new driver knob. One bound: `MAX_PAGE_LIMIT` — the server clamps what a caller may ask for,
because `limit=1_000_000` is how this happened.

`_SCAN = 1_000_000` is **deleted**, and a guard test fails if any `app/core/` file re-introduces
an unbounded `list(...)`. A constant that large is not a limit; it is a scan wearing a limit's
clothes.

### (3) What production needs that the report omits

| Need | Why |
| --- | --- |
| **Indexes** | `find()` alone moves the scan *into* Postgres. `value->>'tenant' = $1` against JSONB with no index is still sequential. Without an index, "the full-table scan disappears" is false — it becomes "the scan stops crossing the process boundary", which is real but smaller |
| **A keyset cursor, not `offset`** | `offset` is O(offset) in the database and **unstable**: an insert between pages silently skips a row. search-service already does this correctly with an opaque cursor; adding `offset` to the port would invite every service to regress |
| **A total order** | sort key + `id` tie-break, or pagination duplicates and skips rows that share a sort value |
| **A clamped `limit`** | the caller may not choose 1 000 000 |
| **`where` is equality-only** | a free-form `where` is a query DSL. Every DSL in this repo is structured and non-evaluating (analytics, search, authz, connector). Field == value, or value ∈ list. No operators, nothing to inject |
| **`sort` is allowlisted** | otherwise you sort by an arbitrary JSON key with no index behind it |
| **An empty `where` must be explicit** | `find(collection, {})` returning every row is a cross-tenant leak one typo away. Require a non-empty filter, or an explicit `all=True` |
| **`list()` renamed** | keep a full-collection sweep for cron/sweepers, but call it `scan_all()` so it reads like the hazard it is. Otherwise the footgun survives the fix |
| **A materialized ledger balance** | `find()` does not fix (5). The balance must be a projection maintained atomically — exactly what wallet-service already does — with a `reconcile()` that proves it equals the sum of the rows |

### (4) The correctness footgun

> **A truncated scan does not fail. It answers.**

`LIMIT` without `ORDER BY` returns whatever the storage engine yields first. So past the cap:

* a tenant sees **an arbitrary subset** of its files, possibly none, and nothing errors;
* `LedgerStore.record()` sums that arbitrary subset and writes the result into an **immutable
  balance row**.

The bug is not that the scan is slow. It is that the scan is *plausible*. Nothing in the
response distinguishes "you have three files" from "you have three of your eleven thousand
files, and we picked which three at random".

Runner-ups, each tested: `offset` pagination skipping a row under concurrent insert · a page
boundary splitting rows with an equal sort key · `find` with an empty filter returning another
tenant's rows · a `limit` above the clamp · a `sort` on an unindexed field · a memory adapter
whose `find` semantics differ from Postgres'.

---

## 2. The primitive

```python
async def find(
    self,
    collection: str,
    where: dict[str, Any],          # equality only: field == value, or value in [..]
    *,
    limit: int = 100,               # clamped by the caller's repo, never by the caller
    after: str | None = None,       # opaque keyset cursor — NOT an offset
    sort: tuple[str, str] = ("created", "desc"),   # (field, direction), allowlisted
) -> tuple[list[dict[str, Any]], str | None]:      # (rows, next_cursor)
    ...
```

* **memory adapter** — a comprehension, a sort, a slice. Three lines, as the report says.
* **postgres adapter** — pushed down:
  `WHERE collection=$1 AND value @> $2::jsonb AND (value->>'created', id) < ($3,$4)
   ORDER BY value->>'created' DESC, id DESC LIMIT $5`
  with a supporting index. The `@>` containment operator uses a GIN index; the sort uses an
  expression index.
* Both adapters are held to **one contract test**, run against a real PostgreSQL, because a
  `find` that behaves differently on the two drivers is worse than no `find`.

Indexes ship with the adapter, created in `_ensure()`:

```sql
CREATE INDEX IF NOT EXISTS kv_value_gin  ON kv_store USING gin (value jsonb_path_ops);
CREATE INDEX IF NOT EXISTS kv_created    ON kv_store (collection, (value->>'created') DESC, key DESC);
```

## 3. Steps

| # | Step |
| --- | --- |
| 28.1 | `core/cursor.py` — keyset cursor (lift the proven one from search-service; opaque, exact round-trip) |
| 28.2 | `find()` on the DataStore port + memory adapter |
| 28.3 | `find()` on the postgres adapter, with the indexes, pushed down |
| 28.4 | Contract tests for `find()` — memory AND real postgres, identical semantics |
| 28.5 | Rename `list()` → `scan_all()`; bound it; make every remaining caller say why |
| 28.6 | Migrate file-service: `list(tenant)` → `find({"tenant": t})`. Delete `_SCAN`. |
| 28.7 | Migrate the other 11 services (57 call sites), service by service, tests green each time |
| 28.8 | **billing:** materialized ledger balance + `reconcile()`; `UsageStore.aggregate` by keyed rollup |
| 28.9 | Guard test: no `app/core/` file may call `scan_all()` without an explicit `# sweeper:` justification; no constant above `MAX_PAGE_LIMIT` |
| 28.10 | Benchmarks: rows-crossing-the-boundary flat in foreign-tenant count; ledger write O(1) |
| 28.11 | Verify + gate + postgres + live boot + docs + memory |

## 4. Out of scope

Full-text search (search-service owns its own index). Aggregations beyond `count`/`sum` on a
keyed rollup — analytics owns those, and already does them correctly.

## 5. Credit

The report was accurate in every particular, including the part that mattered most: the
priority. *"The `find()` primitive is the fix. The `1_000_000` constant is the symptom."*

What it did not find — `LedgerStore.record()` computing a **balance** from the same truncated
scan — is the reason this phase is not a performance ticket.

---

**STATUS: 🟢 COMPLETE. All 17 services migrated (28.1–28.11). `_SCAN` exists nowhere.**

Done: the opaque keyset cursor (at `app/adapters/datastore/cursor.py` — *not* `app/core/`, which
search-service already owns for its ranking cursor), `find()` + `scan_all()` on the port, the
memory adapter, the postgres adapter with pushdown and its own matching indexes, every request
path in every service, the materialized ledger balance with `reconcile()`, and a new
`safety:scans` gate (10 gates now; `safety:ci-parity` proves the script and the workflow agree).

Full suite: **3 026 passing** across 18 test roots, 23 skipped (infra-gated).

Four defects were found by doing the migration, none of them performance:

* **identity** `revoke_family` — a truncated revoke leaves a stolen refresh token valid.
* **authz** — the PDP compiled its policy set with a `1_000_000` limit *argument*; grep could
  never have found it, and the gate did on its first run.
* **compliance** `add_proof` — pushing the sort into the datastore removed the accidental
  stability of Python's sort, so same-tick consent proofs came back in uuid order. A regulator
  would have been shown a withdrawal preceding the grant it followed.
* **billing** `LedgerStore.record` — a running balance summed from a truncated scan and written
  into an immutable row.

And one in the tests themselves: ten `test_hot_path.py` counting fakes hooked `list()`. When
production moved to `scan_all()` they kept passing while measuring nothing — the same silent
failure as the asyncpg guard test that stopped testing once asyncpg was installed.

**Verified on real PostgreSQL (100 000 rows, 20 tenants):**

```
Index Scan using kv_created_desc on kv_store
  Index Cond: (collection = 'files')
  Filter: (value @> '{"tenant": "t07"}')
Sort node: False    Seq Scan: False
page 1: 2.17 ms     page 2 via cursor: 2.19 ms
```

Page 2 costs what page 1 costs. An `OFFSET` would grow with the page number.

**A bug the database found, that no unit test could.** The first version of the adapter
indexed `(value->>'created')` but ordered by `COALESCE(value->>'created','')`. Those are
different expressions, so Postgres **could not use the index to order**: it bitmap-scanned every
one of the tenant's rows and top-N sorted them — `Bitmap Heap Scan, 5 000 rows read, Sort,
3.44 ms` against `Index Scan, no sort, 0.92 ms`. Correct output, O(tenant's rows) instead of
O(page). The index expression must match the ORDER BY expression exactly, COALESCE and all.

**Also found while writing the fix:** file-service had **no list endpoint at all**.
`FileService.list()` existed and nothing called it. So the reporter's "every list-my-files call
scans the table" was not reachable *in this service* — the latent bug was real, and the same
pattern **is** reachable in services that do expose list routes. The endpoint now exists, paged.

**Remaining:** 28.7 (11 services, 57 call sites) · 28.8 (the ledger balance — the one that
computes **wrong money** from a truncated scan) · 28.9 (the guard test that stops `scan_all`
returning to a request path).

---

## 9. Release notes — what changed, and what was left on purpose

*"A deliberate sweeper and a forgotten request path look identical from outside."* So here is
the list. Every service is migrated. Every surviving full read is named below, and carries a
`# sweeper:` note in the code that the `safety:scans` gate refuses to let anyone remove.

### Migrated — request paths that no longer read the whole collection

| Service | Repository / method | What changed |
| --- | --- | --- |
| **file-service** | `FileRepository.list` | `find({"tenant": t})`, keyset-paged. A `GET /api/v1/files` route now exists — there was none. |
| **audit-service** | `iterate` / `query` / `verify` / `export` | The chain read pages to exhaustion in `seq` order, uncapped. `verify()` cannot report clean on a partial walk. |
| **billing-service** | `LedgerStore.record` | **The worst bug in the batch.** It summed a truncated scan to compute a running balance and wrote the result into an immutable row. The balance is now a materialized projection advanced under compare-and-swap, and `reconcile()` proves it equals the sum of the rows. |
| | `LedgerStore.list` / `reconcile` | Tenant-scoped `find()`, paged. |
| | `_Repo.list_tenant` | One `find()` — inherited by invoices, subscriptions, payments, settlements. |
| | `SubscriptionStore.active_for` | **Entitlement hot path.** Tenant + status pushed down. Past the old cap it could tell a paying customer it had no subscription. |
| | `PaymentStore.by_provider_id` | Webhook resolution by indexed lookup. An empty `provider_id` now matches nothing *before* the query, not inside the loop. |
| | `UsageStore.aggregate` | Was quadratic in customers: every usage row of every tenant, once per subscription per metric. Now one series, paged. |
| | `SettlementStore.for_invoice` | `find({"invoice_id": …})`. |
| **compliance-service** | all nine reads | Consent, consent **proof**, DSARs, legal holds, RoPA, breaches, data map. Every one was reading every other data subject's records to answer for one. |
| | `HoldStore.active_for` | **Fail-closed path.** A truncated read here concludes "no legal hold" and erases data the law required kept. |
| | `DsarStore.open_for` | The idempotency lookup asked `status NOT IN (completed, refused)`. Inverted to the positive `OPEN_DSAR_STATUSES`, so a status added later is *closed* by default rather than silently *open*. |
| **identity-service** | `RefreshStore.revoke_family` | **The sharpest one.** The response to a detected token reuse. It scanned a million tokens across all users and filtered in Python; past the cap it revoked an *arbitrary* subset, reported success, and left the attacker's stolen token working. Now pushed down and paged to exhaustion. |
| **authz-service** | `PolicyStore.all_for` (new) | The PDP compiled its policy set from `list(tenant, 1_000_000)`. Had a tenant ever exceeded it, the PDP would have made access decisions from an arbitrary subset of its own policies. Pages to exhaustion now. |
| | `RoleStore.list` / `PolicyStore.list` | Tenant-scoped. |
| **webhook-service** | `EndpointStore.subscribers` | **Delivery hot path**, once per event on a cache miss. Tenant + `enabled` pushed down; the wildcard match (`invoice.*`) stays in Python because equality cannot express it — but over one tenant's endpoints. |
| | `DeliveryStore.due` | Reads the two retryable statuses, not the delivered history that dwarfs them. |
| | `DeliveryStore.list` / `by_event`, `InboundStore.list(source)` | Scoped. |
| **feature-flag-service** | `FlagStore.list` / `stale`, `ConfigStore.history` / `list` | `history` read every version of every key to return one key's. |
| **job-service** | `JobStore.list`, `ScheduleStore.enabled` | Whatever the caller filtered by is pushed down. |
| **analytics-service** | `EventStore.for_tenant`, `ReportStore.list` | A funnel used to be computed over an arbitrary sample of the *wrong customers'* events. |
| **connector-service** | connections, specs, runs, DLQ | All four tenant-scoped. |
| **notification-service** | `InAppStore.feed` | `FEED_SCAN_LIMIT` now bounds *this user's* recent items, not how many of everyone's items were read to find them. |
| **wallet-service** | `sweep_holds` / `sweep_grants` | Read the ACTIVE holds/grants — the small, shrinking set they can act on — not every hold ever placed. |
| **zatca-service** | `_rows`, `due_for_retry` | One taxpayer's invoices, never another's. The resubmission sweeper reads the backlog, not the archive. |
| **tenant-service** | `list` | Kept (see below). |

### `scan_all` calls kept on purpose — 18 of them, and why

| Where | Why reading every row IS the operation |
| --- | --- |
| `audit-service` `partitions` → `audit_meta` | One row per partition, not per event. **The only `scan_all` in audit-service.** |
| `billing-service` `PlanStore.all` | The plan **catalogue**: one row per plan, not per tenant. No tenant to scope by. |
| `job-service` `due_index` / `leased_index` | `jobs_due` and `jobs_leased` hold one row per *claimable* / *in-flight* job and are emptied as jobs finish. Reading them whole is how a worker finds work. |
| `job-service` `counts`, `list()` unfiltered | Operator metrics and the operator's queue view. Not reachable from a tenant route. |
| `job-service` `ScheduleStore.list` | The cron catalogue. |
| `feature-flag-service` flags (archived) / segments / maintenance | Global catalogues. A deployment with 10 000 feature flags has a different problem than a slow read. |
| `analytics-service` `prune_before` | **Retention.** Visiting every event is the job. |
| `analytics-service` `scan_prefix` | A *prefix* match is not equality, and `find()` speaks equality only by design. Reads pre-aggregated rollups, never raw events. |
| `webhook-service` `InboundStore.list()` unfiltered | An operator inspecting inbound traffic across sources. |
| `notification-service` templates / deliveries | Global template catalogue; operator delivery view. |
| `tenant-service` `list` | This *is* the tenant catalogue. There is no tenant to scope it by. |

Every one is bounded by a named constant (`_CATALOG_MAX`, `_INDEX_MAX`, `_PRUNE_MAX`, …) between
1 000 and 100 000 — one to three orders of magnitude below the old blanket `1_000_000`. The point
is not that the smaller number is safe. It is that a truncation at 10 000 catalogue rows is a
paging bug an operator *sees*, while a truncation at 1 000 000 tenant rows is an arbitrary subset
a *customer* sees.

### The constant is gone everywhere

`_SCAN = 1_000_000` no longer exists in any service, and `1_000_000` may not appear as a literal
in any `app/core/` file. `authz-service` was passing it as a *limit argument* rather than storing
it in a constant — grep would never have found that. The gate did.

### The new gate: `safety:scans`

`tools/ci/check_scan_discipline.py`, wired into `run_all_gates.sh` and `ci.yml` (10 gates now, and
`safety:ci-parity` proves both agree). It parses the AST of every `app/core/` file and fails on:

1. a `scan_all()` with no `# sweeper:` justification,
2. a scan bound above 100 000,
3. `find(collection, {})` — an empty filter is a cross-tenant leak one typo away,
4. the literal `1_000_000` anywhere in code (docstrings and comments may name it).

It found problems in **12 of 17 services** on its first run, including the authz one above.

### What the tests could not have caught, and now can

The counting fakes in ten `test_hot_path.py` files hooked `list()`. When production moved to
`scan_all()` they kept passing while measuring nothing — the same silent failure as the asyncpg
guard test that stopped testing once asyncpg was installed. They now hook `scan_all()`, and
`job-service`'s benchmark caught the first regression within a minute of the change.

### Two things `verify()` now guarantees

1. The chain is walked to **exhaustion**, in `seq` order, on both adapters.
2. A pass means the walk **reached the head** the writer recorded. It no longer asserts
   `count == head_seq`, because retention purge deliberately removes an old prefix — a hole in
   the middle is caught by the `prev_hash` walk, a missing tail by the head check.

**Why the ordering needed a new field.** In Postgres `value->>'seq'` is **text**, so `'10'`
sorts before `'9'` and the chain is walked out of order — verification fails at the second
event on an intact chain. Events now carry `_seq_key`, a zero-padded `seq`, which sorts
identically as text in both adapters. Verified on a real PostgreSQL: 1 500 events, contiguous
and ascending, tamper at seq 750 caught.

### Migration caveat for a live deployment

Chains written before this phase carry no `_seq_key`, and rows written before it carry no
`created` where a repository now sorts by one. No data exists yet, so nothing is being asked of
anyone — but a deployment that predates this phase needs a one-time backfill before `verify()`
will walk its chain.
