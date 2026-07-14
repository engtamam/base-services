# wallet-service

> Self-contained, plug-and-play **FastAPI** service. Backend + endpoints only (no UI).

A prepaid balance you can hold, spend, refund and expire. **Optional** — most SaaS never sells
credits. `billing-service` never imports it, never calls it, never knows it exists. Deploy it
only if you sell credits; everything else keeps working if you don't.

---

## Funds are configuration, not code

Nothing in this service says "promo" or "cash". One knob defines the funds:

```bash
WALLET_FUNDS="promo:1:untaxed,cash:2:taxed"     # name : spend-priority : taxability
WALLET_FUNDS="credits:1:untaxed"                # a plain credits wallet
WALLET_FUNDS="gift:1:untaxed,prepaid:2:taxed,cash:3:taxed"
```

An **untaxed** fund is a **pre-VAT discount** — a gift carries no VAT. VAT is charged on the
taxable remainder; the **taxed** funds cover the resulting gross. Spend `1000` with `400` promo
at 15%: promo discounts to `600`, VAT is `90`, cash pays `690`. Charging VAT first would take
`750` from cash and overcharge the customer by `60`.

An unparseable `WALLET_FUNDS` fails at boot with `ConfigError`. Money is **integer minor units**
(halalas/cents) everywhere; a float would lose a halala per operation and break reconciliation.

## The three things a wallet gets wrong

| Footgun | What this does |
| --- | --- |
| **The balance drifts from the ledger** | the ledger is the truth, the wallet row is a projection; both move in ONE compare-and-swap, and `reconcile()` proves `balance == sum(ledger)` |
| **Spending money that is already held** | spends are planned against `available = balance − held`, so a reservation is real, not decoration |
| **Refunding a gift into cash** | a refund restores the funds **from the original spend's ledger row** — an expiring gift never silently becomes real money |

Balance is a *derived* quantity, but summing the ledger on every read is an O(n) hot path that
grows forever. So it is materialized — and then continuously proven.

## Holds expire

`reserve → confirm | release`. A hold carries `expires_at`, and `sweep-holds` enforces it. This
is the leak the reference implementation we studied actually has: its reservations have no
expiry and only two explicit cancel paths, so a phone that dies between authorize and submit
locks those credits **forever**, while the balance still displays them.

An expired-but-unswept hold is never charged. A confirmed hold cannot be released — that needs a
refund. Confirming twice charges once.

## Hot path

* **Balance read** — exactly one keyed read. Zero scans, and identical cost at 5 or 500 ledger
  entries and at 0 or 50 active holds (reserved totals live in the wallet row).
* **Spend** — one compare-and-swap plus one ledger append. Zero scans, at any table size.
  Idempotency is a *keyed* lookup, not `SELECT … WHERE source = ?` over the ledger.
* **50 concurrent spends of the last unit ⇒ exactly one winner**, balance never negative.

The sweepers scan. They are cron jobs, deliberately not the hot path.

## Endpoints

Money moves **only** on the internal tier. A customer who could credit their own wallet would
print money, so the user tier is read-only by construction.

| Method | Path | Notes |
| --- | --- | --- |
| POST | `/internal/wallet/credit` | only once the PSP has **settled**; an intent is not money |
| POST | `/internal/wallet/spend` | direct debit; `402` rather than overdraw |
| POST | `/internal/wallet/reserve` | returns `hold_id`, `expires_at` |
| POST | `/internal/wallet/confirm` | consume a hold (replay-safe) |
| POST | `/internal/wallet/release` | free a hold; nothing was debited |
| POST | `/internal/wallet/refund` | reverse a spend into its **original funds** |
| POST | `/internal/wallet/sweep-holds` | CronJob — releases abandoned holds |
| POST | `/internal/wallet/sweep-grants` | CronJob — claws back unused expired grants |
| GET | `/internal/wallet/{tenant}` · `/reconcile` | balance · the invariant |
| GET | `/api/v1/wallet` · `/api/v1/wallet/ledger` | own balance · paginated statement |

Capabilities: `platform.wallet`, `wallet.manage`, `wallet.read`.

## Grants

`credit(..., expires_at=…)` records a grant. `sweep-grants` claws back only the **unused
remainder** — the customer keeps what they spent — and never touches money that is currently
reserved for an in-flight action.

## Optional integration

Emits `wallet.credited` and `wallet.spent`. `billing-service` invoices them **if it is
listening**; the wallet is fully functional with no bus at all.

## Run

```bash
ENV=development INTERNAL_SERVICE_TOKEN=<40+ chars> \
  WALLET_FUNDS="promo:1:untaxed,cash:2:taxed" \
  python -m uvicorn app.main:app --port 8017

python -m pytest -q          # 195 tests
python ci/security_gate.py   # license / SAST / secrets / CVE
```

Deploy with the shared chart: `deploy/services/wallet-service.values.yaml` already schedules
both sweepers. **Without those CronJobs, holds never expire.**

## Rate limiting (every request, config-driven)

Sliding-window (GCRA) limiter — **no boundary burst**, and **atomic** (a check-and-increment race
cannot exceed the limit). Every response carries `RateLimit-Limit/Remaining/Reset`; a 429 also
carries `Retry-After`. On a Redis outage it **degrades to a per-pod limiter** (coarser, still
limiting) rather than failing open or 500-ing.

| Choice | Env knob | Options / example |
|--------|----------|-------------------|
| Backend | `RATELIMITER_DRIVER` | `memory`(per-pod) · `redis`(shared) |
| Budget | `RATELIMIT_MAX` / `RATELIMIT_WINDOW_SECONDS` | e.g. `100` / `60` |
| Key | `RATELIMIT_KEY_PARTS` | `subject,ip` · add `tenant`/`route` |
| Per-route budgets | `RATELIMIT_ROUTES` | `[{"prefix","class","limit","window_s","cost"}]` |
| Only count failures | `RATELIMIT_COUNT_STATUSES` | e.g. `401` (a 403 will not lock a user out) |
| On Redis outage | `RATELIMIT_DEGRADE` | `local`(default) · `closed` · `open` |

> The subject is resolved **inside** the limiter (its own optional dependency), not read from
> pre-set request state — so an authenticated request keys per-subject regardless of the route's
> dependency order. No "one office NAT shares a bucket" surprise. Enforced by `safety:edge`.

Operator unblock (never `FLUSHALL`): `POST /internal/ratelimit/unblock {"key":"<key>"}`.

> The public edge (Caddy) is generated from `deploy/edge/edge.json` by `tools/edge/render.py` —
> one config source, injection-refused, HSTS only under TLS, non-root container. `nginx` is a
> future renderer of the same shape.
