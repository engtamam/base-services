# Phase 26 — wallet-service (optional, plug-and-play)

> A prepaid balance you can hold, spend, refund and expire. **Optional**: most SaaS never sells
> credits. billing-service never imports it, never calls it, never knows it exists. Deploy it
> only if you sell credits; everything keeps working if you don't.
>
> Zero-hardcoded: the *funds* themselves are config. Nothing in the code says "promo" or
> "cash". No currency, no VAT rate, no credit names.

Studied (files read, not remembered):
* `Security-tools` (kobo) `channels/cloud_api/store.py` — dual-fund wallet, `spend_wallet`
  (promo first as a pre-VAT discount, cash covers the gross, never negative, fail-closed),
  idempotency on `source`, append-only `wallet_ledger`, `sweep_expired_promos` clawback.
* `personal-care-company/hair-analysis-platform` —
  `credits_balance_reserve/confirm/release`, `SELECT … FOR UPDATE`, available =
  `balance − Σ pending`, single-use RS256 scan token bound to `scan_id`.

Each has what the other lacks. kobo has money, VAT, idempotency and an expiry sweep, but **no
holds**. hair has holds, but **nothing ever releases an abandoned one** — `credits_balance_release`
has exactly two call sites, both explicit cancel paths, and reservations carry no `expires_at`.
A phone that dies between authorize and submit locks those credits forever, while the balance
still displays them.

---

## 1. Definition of Done — answered BEFORE any code

### (1) Hot path

* **Balance read** sits in front of every spend. **"Balance is derived, never stored" is the
  right invariant and the wrong implementation** — summing the ledger is an O(n) read that
  grows forever. So: append-only ledger **plus** a materialized balance row, written in ONE
  atomic step, and `reconcile()` recomputes from the ledger and must agree.
  * **Benchmark:** a balance read is **exactly 1 keyed read, 0 scans**, and identical at 5 or
    500 ledger rows and at 0 or 50 active holds (reserved totals live in the wallet row —
    deriving them from the holds table would put a full scan on every spend).
  * **There is deliberately NO balance cache.** A stale balance lets two pods both approve the
    last spend, which is exactly what the compare-and-swap exists to prevent. Caching money is
    the same mistake as caching a usage counter (Phase 25). One keyed read is already O(1).
* **Spend / reserve** ⇒ **1 atomic op + 1 ledger append, 0 scans**, at any table size.
  * **Benchmark:** 50 concurrent spends of the last unit ⇒ **exactly one succeeds**; the
    balance is never negative; the ledger has exactly one debit. A replayed spend does
    **0 writes, 0 CAS, 0 scans**.

**Forces a second port primitive.** `incr_if_below` (Phase 25) increments one counter. A spend
mutates *several funds at once* and appends a row, so it needs compare-and-swap:
`cas(collection, key, expected_version, value) -> bool`. Memory: the lock. Postgres:
`UPDATE … WHERE (value->>'version')::int = $expected`. Caller retries on conflict, with
**full-jitter backoff** — see §5: without it, 100 concurrent spends on one wallet produced 33
needless `409`s against real PostgreSQL.

**A real fix over kobo:** its `_source_seen` runs `SELECT … WHERE source=?` against the ledger.
Postgres has an index there; our generic DataStore does not, so that is a **full ledger scan on
every spend**. Idempotency here is a **keyed** lookup (`{tenant}|{type}|{source}`), never a scan.

### (2) Every config knob has a consumer

`WALLET_FUNDS` is the plug-and-play core:

```
WALLET_FUNDS="promo:1:untaxed,cash:2:taxed"      #  name : spend-priority : taxability
```

The code never names a fund; it iterates them by priority. A single-fund wallet is
`WALLET_FUNDS="credits:1:untaxed"`. Plus `CLOCK_DRIVER`, `WALLET_CURRENCY`, `WALLET_VAT_BP`,
`HOLD_TTL_SECONDS`, `HOLD_SWEEP_LIMIT`, `GRANT_SWEEP_LIMIT`, `WALLET_IDEMPOTENCY_TTL_SECONDS`,
`LEDGER_LIST_LIMIT`, `CAS_MAX_RETRIES`, `CAS_BACKOFF_MS`. Each wired before it ships;
`test_every_settings_field_is_consumed_somewhere` enforces it.

There is deliberately **no `ALLOW_NEGATIVE_BALANCE`** — a switch that lets the wallet go
negative is one someone flips during an incident and never flips back.

### (3) What production needs that the checklist omits

| Need | Why |
| --- | --- |
| **Hold expiry sweeper** | hair's live leak: a hold with no expiry and no sweeper locks credits forever while the balance still shows them |
| `available = balance − active holds` | otherwise `spend` happily spends money that is already reserved |
| **`reconcile()`** | two writes, one truth: the materialized balance must be provably equal to `sum(ledger)` |
| **Integer minor units** | floats lose halalas; money is `int` everywhere, and VAT rounding is stated once |
| **Keyed idempotency + TTL**, replay returns the ORIGINAL result | at-least-once bus; a replay must not re-apply, and an unbounded dedupe table never forgets |
| **Top-up credits only on PSP settlement** | kobo's rule: recording an intent is not money |
| **Expiring grants + clawback sweep** | an unused gift must expire; the customer keeps what they spent |
| **Bounded ledger pagination** | a statement endpoint that returns the whole ledger is a DoS |
| **Refund / clawback** | a failed action must return the money |
| `wallet.spent` event | billing invoices it **if listening**; wallet works standalone |

### (4) The correctness footgun

Three. The third is the one nobody sees coming.

1. **The stored balance drifting from the ledger.** Two writes, one truth. They move together,
   atomically, or `reconcile()` catches it.
2. **Spending held money.** If `spend` checks `balance` rather than `balance − holds`, a
   reservation is decoration and the wallet oversells.
3. **Refunding into the wrong fund.** A spend that consumed expiring promo credit, refunded to
   cash, silently converts a gift that was about to expire into real money. A refund must
   restore **per-fund, from the ledger row that recorded the spend** — never to a default fund.

Runner-ups, each tested: VAT charged on a promo discount (promo is *pre-VAT*, it carries no
VAT) · a replayed `confirm` charging twice · a hold outliving its grant · negative amounts ·
`reserve` succeeding on an expired hold's credits.

---

## 2. Model

Money is **integer minor units** (halalas/cents) everywhere. Funds come from config.

```
wallet:{tenant}          -> {version, funds: {promo: 500, cash: 12000}, updated}
hold:{id}                -> {tenant, amount, fund_plan:[...], expires_at, state}
ledger:{tenant}:{seq}    -> immutable row {type, fund, net, vat, amount, balance_after, source}
idem:{tenant}|{t}|{src}  -> the ORIGINAL result (bounded TTL)
```

`spend` walks the funds in priority order: an untaxed fund applies as a **pre-VAT discount**,
VAT is charged on the taxable remainder, and the taxed funds cover the resulting gross. It
**never** drives a fund below zero — insufficient funds raises and mutates nothing.

## 3. API

Internal (`X-Internal-Token`):

| Method | Path | Semantics |
| --- | --- | --- |
| GET | `/internal/wallet/{tenant}` | balance, available, active holds |
| POST | `/internal/wallet/credit` | settle a top-up / grant (idempotent by `source`) |
| POST | `/internal/wallet/reserve` | place a hold; returns `hold_id`, `expires_at` |
| POST | `/internal/wallet/confirm` | consume a hold → ledger debit (idempotent) |
| POST | `/internal/wallet/release` | cancel a hold; money returns to its funds |
| POST | `/internal/wallet/spend` | direct debit, no hold (idempotent by `source`) |
| POST | `/internal/wallet/refund` | reverse a spend **into the funds it came from** |
| POST | `/internal/wallet/sweep-holds` | release expired holds (CronJob) |
| POST | `/internal/wallet/sweep-grants` | claw back expired grants (CronJob) |
| GET | `/internal/wallet/{tenant}/reconcile` | `balance == sum(ledger)` or it fails loudly |

User tier: `GET /api/v1/wallet` (own balance), `GET /api/v1/wallet/ledger` (paginated).
Capabilities: `platform.wallet` · `wallet.manage` · `wallet.read`.

## 4. Steps

| # | Step |
| --- | --- |
| 26.1 | Service skeleton (`create_service.py`) + config (`WALLET_FUNDS` parsing, fail-fast) |
| 26.2 | `core/money.py` — integer minor units, VAT split, no floats |
| 26.3 | `core/funds.py` — ordered funds from config; the spend plan (untaxed = pre-VAT discount) |
| 26.4 | `cas()` on the DataStore port + memory/postgres adapters |
| 26.5 | `core/ledger.py` — append-only rows, keyed idempotency, derived-balance `reconcile()` |
| 26.6 | `core/wallet_service.py` — credit / spend / reserve / confirm / release / refund |
| 26.7 | Hold + grant expiry sweepers |
| 26.8 | Routes + manifest + `wallet.spent` event (optional consumer) |
| 26.9 | Tests: 50-concurrent-spend ⇒ one winner, never negative · balance == sum(ledger) · hold expiry releases · available excludes holds · refund returns to the ORIGINAL funds · promo carries no VAT · replayed confirm charges once · knob guard · benchmarks |
| 26.10 | Verify + gate + live boot + deploy values + docs + memory |

## 5. Out of scope

Payment gateways (billing-service owns PSPs; wallet takes a *settled* top-up). Invoicing and
VAT filing (billing/zatca). Plan limits (Phase 25 — that is entitlements, not money).

---

**STATUS: ✅ ALL DONE — 195 wallet tests, gate PASS, live verified. billing-service untouched (229 still green). Deploy guards 143 green across 17 services; wallet's two sweeper CronJobs are wired and asserted by `test_the_maintenance_endpoints_we_shipped_have_a_scheduled_caller`.**

Built: `core/money.py` (integer minor units, half-up VAT, `gross == net + vat` exactly) · `core/funds.py` (fund-name-agnostic spend plan; untaxed = pre-VAT discount) · `cas()` on the DataStore port + memory/postgres · `core/wallet_service.py` (credit/spend/reserve/confirm/release/refund/reconcile + two sweepers) · read-only user tier · `WALLET_FUNDS` parsed fail-fast.

**Measured:** 50 concurrent spends of the last unit ⇒ **exactly 1 winner**, balance 0, reconcile consistent. Balance read = **1 keyed read, 0 scans**, identical at 5 or 500 ledger rows and at 0 or 50 holds. Spend = **1 CAS + 1 append, 0 scans**, identical cost at 5 or 500 prior spends. A replayed spend = **0 writes, 0 CAS, 0 scans**.

**Design corrections made mid-build (both caught before shipping, both would have been silent):**
1. The first `_plan` derived reserved totals by `store.list(_HOLDS)` — a **full scan of the holds table on every spend**, killing the "0 scans" claim. Reserved totals moved *into* the wallet row, so `available = funds - held` costs nothing.
2. The first `_commit` did `cas(wallet)` then a separate `put(ledger)`. A crash between them drifts the balance from the ledger forever. The ledger row is now **staged inside the same CAS** (`wallet.pending`) and replayed by the next write or by `reconcile()` — money never moves without its record. A test kills the append and proves the next write repairs it.

**Kept from kobo:** dual-fund promo/cash, promo as a pre-VAT discount, never-negative fail-closed spend, idempotency on `source`, append-only ledger, expiry clawback that keeps what was spent.
**Kept from hair:** `reserve → confirm | release`.
**Fixed in both:** kobo's `_source_seen` is a `WHERE source=?` ledger lookup (indexed there, a full scan on our generic DataStore) → keyed idempotency. hair's reservations have no `expires_at` and no age-based release → **holds expire and a bounded sweeper enforces it**.

**VERIFIED ON REAL POSTGRESQL (16.4, docker).** The DataStore contract runs against a live server whenever `DATASTORE_TEST_DSN` is set. `cas()`: 50 concurrent writers on the same version → **exactly one wins**; 30 concurrent creators of the same new key → **exactly one wins**. Full `WalletService` on Postgres: 50 concurrent spends of the last unit → **1 winner, 49 × InsufficientFunds, balance 0, reconcile consistent**; holds, confirm, refund-to-original-funds and the abandoned-hold sweep all behave identically to the memory adapter.

**A real defect the Postgres run exposed — and the reason running it mattered.** The CAS retry loop had no backoff: every loser retried in the same instant and collided again. Measured, 100 concurrent spends on ONE wallet:

| retries | backoff | succeeded | HTTP 409 |
| --- | --- | --- | --- |
| 8 (shipped default) | none | 17 | **33** |
| 12 | 4 ms | 48 | 52 |
| 20 | 12 ms | **100** | **0** |

Every 409 was *safe* — a losing CAS writes nothing and `reconcile()` stayed consistent throughout — but it is a retry the caller should never have had to make. Fixed with full-jitter backoff (`CAS_BACKOFF_MS`, a knob with a consumer) and defaults moved to the measured values (`CAS_MAX_RETRIES=20`, `CAS_BACKOFF_MS=12`). The first attempt never sleeps, so an uncontended spend is unaffected. **This was invisible on the in-memory adapter**, where an `asyncio.Lock` serialises writers instead of letting them race.
