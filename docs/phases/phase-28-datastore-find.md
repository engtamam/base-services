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

**STATUS: 🟡 PRIMITIVE DONE + file-service migrated. 11 services and the ledger remain.**

Done (28.1–28.6, 28.10): `core/cursor.py` (opaque keyset), `find()` + `scan_all()` on the port,
memory adapter, postgres adapter with pushdown and its own indexes, `_SCAN` deleted from
file-service, a `GET /api/v1/files` route that pages by cursor, `MAX_PAGE_LIMIT` clamped
server-side, 16 new tests. file-service: 157 green.

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
the list.

### Migrated

| Service | Repository | What changed |
| --- | --- | --- |
| **file-service** | `FileRepository.list` | `find({"tenant": t})`, keyset-paged. `_SCAN` deleted. A `GET /api/v1/files` route now exists — there was none. |
| **audit-service** | `AuditRepository.iterate` | **The chain read.** Pages to exhaustion in `seq` order. No cap. |
| | `AuditRepository.query` | Equality filters pushed down; `since`/`until` post-filter over pages, never over the table. |
| | `AuditRepository.partitions` | Reads the tiny `audit_meta` collection (one row per partition) instead of scanning every event to collect a handful of strings. |
| | `AuditService.verify` | Now **cannot report clean on a partial walk** (see below). |
| | `AuditService.export` | Was `query(filters, 1_000_000)`. Now bounded by `AUDIT_EXPORT_LIMIT`. |

### `scan_all` calls kept on purpose

| Where | Why |
| --- | --- |
| `AuditRepository.partitions` → `scan_all("audit_meta", 10_000)` | one row per partition, not per event. A partition list is bounded by the number of tenants, and there is no filter to push down. |

Nothing else in audit-service touches `scan_all`. `list()` remains as a deprecated alias so
existing sweepers in the other services keep working while they are migrated.

### Not yet migrated — still scanning

billing (10) · compliance (9) · feature-flag (9) · job (6) · webhook (6) · analytics (4) ·
connector (4) · notification (3) · authz (2) · wallet (2) · zatca (2) · identity (1) ·
search (1) · tenant (1). Their `list()` calls are unchanged and still read the whole
collection. Nothing got slower; nothing got safer.

**`LedgerStore.record()` (billing) remains the most serious of these**: it sums a truncated
scan to compute a running **balance** and writes it into an immutable row. `find()` does not
fix it — it needs a materialized balance, as wallet-service already has.

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
