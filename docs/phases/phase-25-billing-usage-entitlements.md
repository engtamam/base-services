# Phase 25 — Usage metering & real entitlement enforcement (billing-service)

> The "plan gate" is not a new service. It is a hole in a service we already shipped.
> Plans declare limits; nothing counts usage; the caller supplies its own number.
>
> Everything here is **data, not code**: entitlement keys, their kind, their period and their
> overage policy all live in the plan document. No project's feature names appear anywhere in
> `base-services`. Zero-hardcoded, plug-and-play, deny-by-default.

Studied first: a second reference implementation `channels/cloud_api/store.py` (dual-fund wallet, spend,
idempotency-on-source, promo expiry sweep) and `personal-care-company/hair-analysis-platform`
(`credits_balance_reserve/confirm/release`, `SELECT … FOR UPDATE`, single-use scan token).
Both are **money/credit balances** — that is Phase 26 (`wallet-service`). This phase is the
other half: **plan limits and the usage that counts against them.**

---

## 0. What is actually broken today (verified, with lines)

| # | Finding | Evidence |
| --- | --- | --- |
| 1 | **`current` is caller-supplied.** A tenant calls `GET /api/v1/billing/entitlements/{key}?current=0` and is always allowed. Client-controlled authorization input (CWE-602 / CWE-639). | `app/api/v1/billing.py:216-220` |
| 2 | **Nothing counts usage.** `feature-flag`'s adapter sends no `current`, so it defaults to `0`. Boolean entitlements (`0`=deny) work; every *counted* limit (`seats: 25`) always allows. The limits are declared and unenforced. | `feature-flag-service/app/adapters/entitlement/billing_http.py:26` |
| 3 | **Every check scans the subscriptions table**, then sorts it, then fetches the plan. No cache. `feature-flag` calls this per flag evaluation. Identical to the authz-PDP defect we already fixed once. | `billing_service.py:35-43` → `stores.py:72-73` → `stores.py:28-31` |

Fixing (1) without (2) is impossible: you cannot stop trusting the caller's number until you
have your own.

---

## 1. Definition of Done — answered BEFORE any code

### (1) Hot path — and an honest correction

`check_entitlement` runs before every gated action. Two halves, and they need **opposite**
treatment. Claiming "zero reads" for both would be a lie:

* **Limits are cacheable.** A plan changes on upgrade/downgrade, not per request. Cache the
  resolved limits per tenant, keyed by a **generation counter** bumped on any subscription or
  plan write (the pattern from analytics/authz/feature-flag).
  * **Benchmark:** 1000 × `check()` after warm ⇒ **0 datastore reads, 0 scans, 0 writes.**
    Today it is 1000 full-table scans.
* **Usage is NOT cacheable.** Caching a counter is how two pods both approve the 26th seat.
  `consume()` must read-and-increment **atomically, in the store, every time**.
  * **Benchmark:** `consume()` = **exactly 1 atomic store operation, 0 scans**, at any table
    size. And a concurrency test: 50 concurrent `consume()` against `limit=25` ⇒ **exactly 25
    allowed**, 25 denied. Never 26.

**This forces a port change.** `DataStore` has `get`/`put` only, so "read then write" is a
TOCTOU race: two pods read 24, both write 25, and the 26th seat slips through. Billing's copy
of the port gains `incr_if_below(collection, key, delta, limit) -> (allowed, new_value)` —
atomic. Memory adapter: an `asyncio.Lock`. Postgres adapter: a single
`UPDATE … SET n = n + :d WHERE n + :d <= :limit RETURNING n`. This is exactly what the reference implementation's
`FOR UPDATE` and hair's `credits_balance_reserve` do; a check-then-act cannot be made correct
by caching harder. Self-contained rule permits it: we change **billing's** copy only.

### (2) Every config knob has a consumer

New knobs — `LIMITS_CACHE_TTL_SECONDS`, `USAGE_DEFAULT_PERIOD`,
`USAGE_IDEMPOTENCY_TTL_SECONDS`, `ENTITLEMENT_UNKNOWN_KEY_DEFAULT` — each wired before it
ships, and `test_every_settings_field_is_consumed_somewhere` enforces it.

There is deliberately **no `USAGE_CACHE_TTL_SECONDS`**: usage is never cached (see the hot-path
answer above), so the knob would be a lie. And no `USAGE_ENABLED`: a switch that turns
enforcement off is a switch someone flips in an incident and never flips back. **No knob before
its consumer**, and no knob that only exists to be dangerous.

### (3) What production needs that the checklist omits

| Need | Why |
| --- | --- |
| **Gauge vs counter** (see §2) | recording `+1` for `seats` and never decrementing on user removal makes the limit wrong forever |
| **Period keys, not reset jobs** | usage stored under `…:{period_key}` never needs a rollover cron; a missed cron would otherwise grant a free month |
| **Billing-anchored periods** | "monthly" means *the subscription's* month. A tenant who signed up on the 31st has no 31st in February |
| **Idempotent `consume()`** | our bus is at-least-once; a redelivered consume must not double-charge a quota |
| **Overage policy as data** | `block` \| `allow_and_bill` \| `soft` (warn, allow) — per key, from the plan |
| **Downgrade below current usage** | 40 seats used, plan drops to 25: never silently delete users. `over_limit: true`, block *new* consumption, surface it |
| **Usage read API + `remaining`** | a UI that cannot show "18 of 25 seats" forces the client to count, which is how (1) happened |
| **Release / refund** | a failed action must give its counter unit back, or retries burn quota |
| **Cache invalidation on plan change** | an upgrade must take effect immediately, not after TTL |

### (4) The correctness footgun

> **Conflating a gauge with a counter, and check-then-act.**

`api_calls` is a **counter**: monotonic within a period, resets by period key, `+1` per call.
`seats` is a **gauge**: the *current* value of something owned elsewhere (identity-service).
Increment a gauge and it only ever goes up — delete a user and the seat is gone forever. So:

* `counter` ⇒ `consume(key, amount)` / `release(key, amount)`, keyed by period.
* `gauge` ⇒ `set_usage(key, n)` reported by the owning service, **no period**, idempotent by
  nature (last write wins).
* **Kind is declared in the plan, never inferred.** An unknown kind fails **closed**.

And `check()` is advisory forever. Only `consume()` enforces, because between a `check` that
said yes and the action, another request consumed the last unit. Any caller that does
`if check(): act()` has a race. The API must make the safe path the easy one.

Runner-ups, each with a test: caller-supplied `current` (deleted from the API surface) ·
period boundary in UTC, rendered in tenant tz · double-count on redelivery · fail-open when
billing is unreachable (the port already promises fail-closed — keep it) · unlimited `-1`
vs deny `0` (the existing convention, preserved).

---

## 2. Model — all data, no hardcoded keys

A plan's `limits` stays backward compatible. A **scalar** keeps today's meaning (boolean /
static limit, no period). A **dict** declares a metered entitlement explicitly:

```jsonc
{
  "sso": true,                     // boolean: truthy = on, 0 = deny
  "seats": { "kind": "gauge",   "limit": 25 },
  "api_calls": { "kind": "counter", "limit": 50000,
                 "period": "billing_period",      // day|week|month|billing_period
                 "overage": "allow_and_bill" },   // block|allow_and_bill|soft
  "storage_mb": { "kind": "gauge", "limit": -1 }  // -1 = unlimited (existing convention)
}
```

`sso`, `seats`, `api_calls` are the *example project's* words. `base-services` never mentions
them: keys are opaque strings, kinds/periods/policies come from an allowlist, and an unknown
value fails closed. That is what makes it fit any platform.

Usage rows are keyed, never scanned:

```
usage:{tenant}:{key}:{period_key}     # counter, e.g. 2026-07 or sub_x@2026-07-31
usage:{tenant}:{key}:_                # gauge (no period)
```

Period keys make rollover free: a new period is a new key. No cron, so a missed cron cannot
hand out a free month.

## 3. API

Internal (service-to-service):

| Method | Path | Semantics |
| --- | --- | --- |
| POST | `/internal/billing/usage/consume` | **atomic** check-and-increment; `Idempotency-Key`; returns `{allowed, remaining, limit, over_limit, policy}` |
| POST | `/internal/billing/usage/release` | give a counter unit back (failed action) |
| POST | `/internal/billing/usage/set` | report a **gauge** (identity reports seats) |
| GET | `/internal/billing/entitlements/check` | advisory only; **`current` param removed** — usage read server-side |

User tier (`/api/v1/billing/...`): `GET /usage` (own tenant, with `remaining`),
`GET /entitlements/{key}` — advisory, **no `current` parameter**.

`feature-flag`'s adapter keeps working unchanged: booleans behave identically, and counted
limits now tell the truth instead of always allowing.

## 4. Steps

| # | Step |
| --- | --- |
| 25.1 | `core/entitlements.py` — kinds/periods/policies as allowlists, unknown ⇒ fail closed; keep scalar back-compat |
| 25.2 | `core/periods.py` — period keys, UTC buckets, billing-anchored months (no 31st-of-February) |
| 25.3 | `ports/datastore` + memory/postgres adapters: **atomic** `incr_if_below` (billing's copy only) |
| 25.4 | `core/usage_store.py` — keyed counters/gauges, idempotency window |
| 25.5 | `UsageService` — `consume` / `release` / `set_usage` / `check`, generation-cached limits |
| 25.6 | Fix `_active_limits`: cache by generation, **0 scans**; bump on subscription/plan write |
| 25.7 | Routes + manifest; **remove the `current` query parameter** |
| 25.8 | Overage: `allow_and_bill` emits to the existing overage/ledger path |
| 25.9 | Tests: 50-concurrent-consume ⇒ exactly `limit` allowed · gauge never accumulates · redelivered consume charges once · period rollover · downgrade `over_limit` · caller cannot supply usage · knob guard · benchmark (check = 0 reads; consume = 1 atomic op, 0 scans) |
| 25.10 | Verify + live boot + docs + memory |

## 5. Out of scope

Spendable balances, holds, top-ups, VAT on consumption → **Phase 26 `wallet-service`**
(separate: most SaaS never sells credits, and billing must stay usable without it). The
kill-switch pattern (`integrationGate.js`) is deliberately **not** built: it is a circuit
breaker + a manual override, and we already have `connector-service/app/core/circuit.py` and
`feature-flag-service`. It must never share a code path with entitlements — a kill-switch
fails **open**, an entitlement fails **closed**.

---

**STATUS: ✅ ALL DONE — 229 billing tests (167 pre-existing still green → full back-compat), gate PASS, live verified. feature-flag-service: 198 tests still green (its entitlement adapter reads only `allowed`; boolean behaviour is unchanged).**

Built: `core/entitlements.py` (kinds/periods/policies as allowlisted data, unknown ⇒ fail closed, `present` distinguishes granted-zero from never-mentioned) · `core/periods.py` (period keys; billing-anchored months clamp Jan-31 → Feb-28) · `incr_if_below` on the DataStore port + memory (asyncio.Lock) and postgres (ensure-row, then bounded `UPDATE … RETURNING`) · `core/usage_service.py` (consume/release/set_usage/check/summary) · generation-cached `_active_limits` · internal usage routes · `current` **deleted** from the HTTP surface.

**The three verified defects this closes** (all in shipped code, all with line numbers in §0): the tenant supplied its own usage (`?current=0` passed every limit — CWE-602); nothing counted usage, so every counted limit always allowed; and each check scanned + sorted the whole subscriptions table with no cache, on a path feature-flag calls per flag evaluation.

**Measured:** `naive get+put` under 50 concurrent consumes against a limit of 25 → **50 allowed, final value 1**. With `incr_if_below` → **exactly 25 allowed, final 25**. Limit resolution after warm: **0 reads, 0 scans**, and identical cost at 5 or 500 subscriptions. `consume()`: **exactly 1 atomic op, 0 scans**.

**Corrections made mid-build, both caught before shipping:**
1. First draft recorded `allow_and_bill` overage into `LedgerStore`, whose balance is `sum(amount_minor)` — writing *units* into a money ledger would have corrupted every tenant's balance. Overage units now go to `UsageStore`, which `run_overage()` already aggregates and prices.
2. Postgres `incr_if_below` originally used `INSERT … ON CONFLICT DO UPDATE … WHERE bound`; the `WHERE` guards only the update path, so the **first** call would seed `n = delta` even when that already exceeded the limit. Rewritten as ensure-row-at-zero, then a bounded `UPDATE`.

**VERIFIED ON REAL POSTGRESQL (16.4, docker).** `incr_if_below` runs the DataStore contract against a live server whenever `DATASTORE_TEST_DSN` is set. 50 concurrent callers against `limit=25` → **exactly 25 allowed, final value 25**; 200 concurrent against `limit=100` → **exactly 100**. The first-insert bound (the bug fixed blind during the build) is confirmed: `incr_if_below` on a fresh key with `delta=50, limit=10` returns `(False, 0)`, not `(True, 50)`.
