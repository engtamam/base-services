# payments-service (checkout conduit) — Security & Correctness Critique

**Date:** 2026-09-19 · **Scope:** the entire P4 pay-link conduit in `services/payments-service`
(payment_requests, payments, merchant_credentials, secret_store, payment_gateway + tuwaiqpay/console/null
adapters, payment_http, datastore cas, internal + public routes, config).
**Method:** 5 parallel deep audits (money-correctness/concurrency, tenant-isolation/authz/IDOR,
secrets/crypto, web/SSRF/redirect/DoS, best-practices gap) reading the **actual code line-by-line —
not the docstrings** — plus current (2026) online best-practice research. Nothing here is released;
this is the improvement backlog before the conduit handles real money.

> Recurring theme: several docstrings **assert** a security property the code does **not** enforce
> (no-cross-tenant-fallback, https-only redirect, amount-always-verified, "never reveals whether an id
> exists", per-process KDF). Trust the code; the comments oversold it.

---

## Severity summary

| ID | Sev | Title | File(s) |
|----|-----|-------|---------|
| C1 | **CRITICAL** | Double-charge: `create_link` dedup is a TOCTOU; CAS keyed on a random uuid, not `(tenant,reference)` | payments.py:78-99, payment_requests.py:43-64 |
| H1 | **HIGH** | SSRF via tenant-controlled `MERCHANT_API_BASE` — no egress guard | payment_http.py:20-30, merchant_credentials.py:80 |
| H2 | **HIGH** | A paid order silently marked `expired` → real webhook dropped, reconcile can't recover | payments.py:110-126,171-183, payment_requests.py:115-116 |
| H3 | **HIGH** | Amount verification bypassed when the gateway reports amount `0`/absent → unverified success completes | payments.py:139-142 |
| H4 | **HIGH** (latent) | Cross-tenant credential bleed: merchant store inherits the `"*"` platform-wide row via shared `get()` fallback | secret_store.py:154-161, merchant_credentials.py:10-12,74-80 |
| H5 | **HIGH** | Unauthenticated DoS: scrypt KDF runs **per request** + unbounded body read + no rate limit on public routes | secret_store.py:75-76, payments_deps.py:31-55, payments_public.py:42 |
| M1 | MEDIUM | AES-GCM sealed with **no AAD** → ciphertext relocatable across tenant/name by a DB tamperer | secret_store.py:83-103 |
| M2 | MEDIUM | Webhook amplification + tenant-enumeration timing oracle (per-request re-auth; dead token cache) | payments_public.py:36-51, tuwaiqpay.py:58-74 |
| M3 | MEDIUM | Currency: `fetch_status` may return numeric id → flags every real payment as mismatch; unknown currency at create silently coerced to SAR | tuwaiqpay.py:92,129-131, payments.py:141 |
| M4 | MEDIUM | "First terminal wins" silently drops a later genuine success (failed-then-paid; lost-CAS) | payments.py:143-149, payment_requests.py:115-116 |
| M5 | MEDIUM | Redirect scheme not enforced (https) despite the docstring's claim | payments.py:62-66 |
| M6 | MEDIUM | No background reconciliation/expiry sweep — a missed (email-registered) webhook leaves money paid, order stuck `pending` forever | payments.py (no sweep) |
| M7 | MEDIUM | No refund / chargeback / dispute ingestion — `completed` is terminal forever; a gateway refund is invisible | payment_gateway.py, payments.py |
| M8 | MEDIUM | Blanket `except Exception → 200` on the webhook swallows real bugs silently | payments_public.py:47-48 |
| L1 | LOW | `hint()` leaks last 4 chars — up to ~80% of a short secret | secret_store.py:105-108 |
| L2 | LOW | Console webhook secret compared with `==` (timing oracle; bad pattern to copy) | console.py:45 |
| L3 | LOW | No boot guard for empty `SECRETS_KEK` (unlike PII_SALT) — fails deep on first credential op | config.py:121 |
| L4 | LOW | `provider_ref` interpolated into the gateway URL path unquoted (path/param injection) | tuwaiqpay.py:119 |
| L5 | LOW | `PENDING_SETTLEMENT` mapped to SUCCEEDED — completes before capture | tuwaiqpay.py:33 |
| L6 | LOW | Float money math at the gateway boundary + exponent-2 hardcoded (`/100`) — breaks for KWD/BHD/OMR (exp-3) | tuwaiqpay.py:92,129 |
| L7 | LOW | `amount_minor` has no upper bound | internal_payments.py:79 |
| L8 | LOW | `metadata` is an unbounded dict (stored + forwarded) | internal_payments.py:82 |
| I1 | INFO | Internal token is omnipotent across tenants; `tenant` param is a consistency check, not an authz boundary | internal.py, internal_payments.py |
| I2 | INFO | Redirect 404 vs 410 vs 302 leaks request existence/state to a link holder (docstring says it doesn't) | payments_public.py:21-33 |
| I3 | INFO | `return_url` accepted but dropped by the TuwaiqPay adapter | tuwaiqpay.py:88-101 |
| I4 | INFO | Layering: routes reach into `svc._req` private; `applied_event_id` stored but never used | internal_payments.py:118 |

---

## CRITICAL

### C1 — Double-charge under a concurrent/retried `create_link`
`create_link` (payments.py:78-99) does: `active_by_reference()` read → `gateway.prepare_checkout()`
(mints a **real** gateway bill) → `create()` which `cas(_COLL, rid, 0, row)` where `rid = uuid4()`.
The only atomic guard is CAS on a **fresh random key**, so it protects against a UUID collision, not
against a duplicate order `reference`. There is no unique constraint on `(tenant, reference)`.

**Failure:** the store app retries a slow call, or the customer double-taps. Two concurrent
`create_link(reference="order-42")` both pass `active_by_reference` (neither has inserted yet) → both
mint a gateway bill → both `create()` with different `rid`s → **both CAS succeed** → two live pay-links
and two `payment.completed` events for one order, each amount-matching so nothing downstream flags it.

**Fix:** claim `(tenant, reference)` atomically **before** `prepare_checkout`. Add a deterministic claim
key (tenant-length-prefixed, per the tenant-scoped-idempotency rule) inserted via `cas(claim, key, 0, …)`;
the winner mints + creates, the loser re-reads and returns the winner's request. (Best-practice: persist
the idempotency key, tied to the order, **before** the gateway call.)

---

## HIGH

### H1 — SSRF via `MERCHANT_API_BASE` (no egress guard)
`MERCHANT_API_BASE` is a **tenant-settable** credential used verbatim as the gateway base URL in
`payment_http._send` → `urllib.request.urlopen`, with zero validation: no scheme allowlist, no
private/loopback/link-local block, no redirect cap. `urlopen` will reach `http://169.254.169.254/…`
(cloud metadata), `http://127.0.0.1:port/…` (internal scan), `file://`, etc. Reachable even without the
internal token: the **unauthenticated** `/webhook/{tenant}` route drives an outbound fetch once creds
are set. The docstring "hosts are FIXED config … not the SSRF surface" is false; the `# nosec B310`
suppresses the very scanner that would catch it.
**Fix:** egress guard in `_send` — scheme allowlist (`https`, `http` only under an explicit UAT flag);
resolve host, reject `is_private/is_loopback/is_link_local/is_reserved` (pin resolved IP vs DNS-rebind);
disable/cap redirects. Also validate `MERCHANT_API_BASE` against an allowed-domain list at write time.

### H2 — A paid order can be silently marked `expired`, and the money is unrecoverable
Our TTL (`expires_at`) is independent of the gateway link's real validity. `mark_expired` sets
`EXPIRED`, which is in `_TERMINAL`; `apply_outcome` no-ops on any terminal row. So: customer opens the
link → our TTL elapses → a second redirect hit marks it `EXPIRED` → customer completes on the page they
already had open → the verified `payment.completed` webhook lands on a terminal row → **dropped, no
event, no flag**. And `reconcile` early-returns for any non-`pending` row, so it cannot recover it
either. Money captured, order never fulfilled, silent.
**Fix:** expiry must not be terminal against a confirmed settlement — let a verified `SUCCEEDED`
transition `EXPIRED → COMPLETED` under CAS, and let `reconcile` re-query `EXPIRED` rows; emit a loud
alert when a verified success lands on an already-expired request. Consider gating our TTL by
`expires_at_provider`.

### H3 — Amount guard bypassed on a `0`/absent amount
`mismatch = bool(outcome.amount_minor) and (…)`. A verified `SUCCEEDED` with `amount_minor == 0`
short-circuits to `mismatch=False` and **completes as full success** — the amount *and* the nested
currency check are skipped. `tuwaiqpay.fetch_status` yields `0` when `data.bill` is absent.
**Fix:** "success with no confirmable amount" is *unverifiable*, not *matching* — refuse+flag+alert, or
force a re-query that returns an amount before completing. Never read "no amount" as "amount matches".

### H4 — Cross-tenant credential bleed via the shared `get()` fallback (latent)
`SecretStore.get()` falls back from `(tenant,name)` to `("",name)` (the `"*|…"` platform-wide row).
The merchant store reuses this `get()`, and `_config` reads each `MERCHANT_*` field through it — so a
single platform-wide merchant row is silently inherited by **every** unconfigured tenant (money routed to
the wrong shop), and fields can even be mixed (one tenant's DRIVER + a platform API_KEY). The write API
blocks `tenant=""` **today** (so not reachable via the route now), but a backup restore/remap
(`into_tenant`) or any future writer that lands a `*|MERCHANT_*` row activates it. The docstring
"there is no platform-wide fallback" is false for the code path it relies on.
**Fix:** give the merchant store a fallback-disabled `get()` (flag on `SecretStore`) resolving strictly to
`(tenant,name)`; reject `tenant=""` writes in the merchant path; fail-closed to null when a required
field is absent for that exact tenant.

### H5 — Unauthenticated resource-exhaustion DoS
`SecretStore.__init__` runs `scrypt(n=2^14)` (~tens of ms CPU + 16 MiB) and is constructed **per
request** via `build_merchant_secrets` — so the "derived once" optimization the docstring claims does not
hold at the request layer. Both public routes are unauthenticated and unthrottled; `request.body()` has
no size cap. A few dozen concurrent `GET /pay/{x}` or `POST /webhook/{x}` saturate CPU/memory.
**Fix:** memoize the derived KEK by `(kek,salt)` at process scope (or build the store once on
`app.state`); cap request body size; rate-limit both public routes (per-IP and per-tenant).

---

## MEDIUM

- **M1 — AES-GCM without AAD.** Correct nonce/tag usage, but no associated data binds a ciphertext to
  its slot, and one key protects the whole collection. A DB **tamperer** (no KEK) can copy tenant A's
  `sealed` blob into tenant B's row (or another `name`, or `previous`→`sealed`) and it decrypts fine → B
  authenticates as A. **Fix:** `aad = f"{coll}|{tenant}|{name}"` on seal+open (thread tenant/name in;
  they're available at every call site), optionally a slot marker.
- **M2 — Webhook amplification + tenant-enumeration oracle.** The unauthenticated webhook builds a fresh
  adapter per request (token cache dead), so each hit forces a full `/auth` + `/bills/transaction` GET on
  the tenant's real merchant account → flood exhausts the shop's gateway quota; configured vs
  unconfigured tenants differ in latency (null gateway returns instantly) = a live-merchant enumeration
  oracle. **Fix:** rate-limit; pre-check a pending request exists for `(tenant, provider_ref)` before
  verify-by-fetch; per-tenant short-lived token cache; equalize the fast path.
- **M3 — Currency.** (a) `fetch_status` returns `bill.currency` raw; if TuwaiqPay returns the numeric
  `currencyId` (e.g. `"1"`), `"1" != "SAR"` → **every real payment flagged mismatch and refused**.
  (b) `create_link` accepts any 3-char currency; `_CURRENCY_ID.get(cur, 1)` silently charges unknown
  currencies as SAR. **Fix:** normalize `fetch_status` currency to ISO (reverse-map), add a captured UAT
  fixture; validate `currency` against the supported set at create.
- **M4 — Success loses to a prior terminal state.** A `FAILED`/`DECLINED` (or a lost CAS) that lands
  first makes a later genuine `SUCCEEDED` a silent no-op (failed-then-retried-and-paid on the same link).
  **Fix:** a verified `SUCCEEDED` may supersede a non-success terminal state under CAS, or at minimum
  emit an alert/audit event so ops reconcile — success must never lose silently to failure.
- **M5 — Redirect https not enforced.** `_host_allowed` checks only the host, not the scheme; the
  docstring claims https-only. `http://<allowlisted-host>` (downgrade) passes. **Fix:** require
  `scheme == "https"`.
- **M6 — No background reconciliation/expiry sweep.** `reconcile`/`mark_expired` are lazy/manual only.
  TuwaiqPay webhooks are registered by **email** (the least reliable link), so a missed webhook leaves
  money paid and the order stuck `pending` indefinitely. **Fix:** a periodic sweep that (a) auto-reconciles
  pendings older than N minutes and (b) expires truly abandoned ones. Single biggest robustness gap.
- **M7 — No refund/chargeback/dispute ingestion.** `completed` is terminal forever; a gateway refund or
  dispute is invisible or dropped. **Fix:** an optional `refund()` port capability + append-only
  `refunded`/`disputed` states that never rewrite the `completed` money fact; at least log a dispute.
- **M8 — Blanket webhook `except → 200`.** Correct anti-retry-storm, but it also swallows genuine bugs
  silently (violates the "defensive access hides bugs" rule). **Fix:** log the exception before the 200.

---

## LOW

- **L1** `hint()` — scale the revealed tail to length (e.g. tail only when `len ≥ 12`), not a fixed 4.
- **L2** Console webhook secret — `hmac.compare_digest`, not `==` (so the pattern isn't copied to a real HMAC adapter).
- **L3** Add `assert_secrets_config_ok()` refusing empty `SECRETS_KEK` (and `BACKUP_KEK` if export enabled) at boot.
- **L4** `urllib.parse.quote(provider_ref, safe="")` + charset/length validation before URL interpolation.
- **L5** Confirm `PENDING_SETTLEMENT` is guaranteed-capture with TuwaiqPay; if not, map to `PENDING` and complete only on `SETTLED`.
- **L6** Move gateway money math off `float`; centralize minor↔major via an ISO-4217 exponent table before any exp-3 currency (KWD/BHD/OMR/…).
- **L7** Add a sane `le=` ceiling to `amount_minor`.
- **L8** Cap `metadata` serialized size / key count.

---

## INFO / architecture

- **I1** The internal token grants full cross-tenant reach over requests and full merchant-credential
  **write/clear** (never read). Appropriate for the "consumer platform is the caller" model, but means
  token compromise = credential control-plane compromise. Use the `jwks`/`static` per-caller identity
  mode (not the flat `shared` token) in production; keep credential-mutation routes on a tight per-route
  allow-list. Consider a distinct scope/token for credential mutation vs request creation.
- **I2** Collapse redirect non-302 outcomes to one generic response so a link holder can't distinguish
  "unknown" / "spent-or-expired" / "payable"; fix the docstring either way.
- **I3** Wire `return_url` into the bill payload if TuwaiqPay supports a return/callback URL, else
  document that the customer is not bounced back to the shop.
- **I4** Add a public `PaymentService.get()` (routes currently reach `svc._req`); either use
  `applied_event_id` for per-notification dedupe or document that the state CAS is the dedupe.
- **PCI:** the design is correctly SAQ-A (no PAN, redirect-only). Note the host serving `/pay/{id}`
  inherits SAQ-A redirect-page obligations (script integrity monitoring). SAMA/PSP scope correctly sits
  with the shop + its licensed gateway, not us — keep it that way.

---

## What the audits confirmed is CORRECT (do not "fix")

- **CAS parity** across memory + postgres adapters (insert-if-absent vs version-pinned); the terminal
  transition is a genuine exactly-once single-winner — replays of an identical webhook yield one
  completion + one event.
- **Verify-by-fetch** for TuwaiqPay's unsigned webhook — the body's status is never trusted; the
  authenticated re-query is the trust anchor (aligned with 2026 guidance).
- **Cross-tenant settle is blocked** — `by_provider_ref` and `active_by_reference` are tenant-scoped;
  `get_request`/`reconcile` re-check `row.tenant == tenant`.
- **Server-authoritative amount** (except the H3 zero-hole) — a mismatched settle is forced to failed+flagged.
- **Open-redirect host allowlist is fail-closed** on empty (except the M5 scheme gap).
- **Write-only credential API** — no route returns plaintext; `SETTABLE_NAMES` blocks key smuggling;
  salt separation is real; decrypt fails loud (never fail-open to `""`); rotation window behaves as
  specified; events carry no credentials or links.
- Conduit stays PCI-minimal: no card vaulting, no fund custody, no gateway proxying.

---

## Remediation order (recommended)

1. **C1** atomic `(tenant,reference)` claim before `prepare_checkout` (stops double-charge).
2. **H1** SSRF egress guard in `payment_http` + validate `MERCHANT_API_BASE` at write.
3. **H5** cache the KEK (build the store once) + body-size cap + rate-limit public routes.
4. **H2** expiry non-terminal vs a confirmed success + reconcile recovers expired + alert.
5. **H3** treat 0/absent-amount success as unverified.
6. **H4** disable the platform-wide fallback for the merchant store + reject `tenant=""` writes.
7. **M1–M8** AAD binding, webhook throttle+precheck+token cache, currency normalization+validation,
   success-supersedes-terminal(or alert), https redirect, background reconcile/expiry sweep, refund/dispute
   ingestion, log-before-200.
8. **L1–L8, I1–I4** hardening + layering.

Items C1, H1–H5, M1–M4, M6–M8 are code fixes to land before real money. M6/M7 and per-option locks
overlap with commerce **Phase 6** (wiring/provisioning). None released yet — this is the pre-release
security backlog.

---

## Remediation status (2026-09-19) — FIXED

All findings except M7 were remediated across five commits (C1/H1/H5/M8 inline; then four parallel
worktree tasks with strictly disjoint files, each reviewed line-by-line before merge). Full
payments-service suite: **426 passed, 49 skipped**. Not released (commerce hold).

- **C1, H1, H5, M8** — `9c3e36d6` (atomic (tenant,reference) claim; SSRF egress guard + write-time
  MERCHANT_API_BASE validation; KEK memoized + webhook body cap + rate-limit; log-before-200).
- **H4, M1, L1** — `11389503` (merchant store no-fallback + require_tenant; AES-GCM AAD; hint length).
- **H2, H3, M4, M5, M2, I2, I4** — `46327de7` (expiry non-terminal vs confirmed success + reconcile
  recovers it; 0-amount success refused; success supersedes terminal; https redirect; webhook
  junk-id short-circuit; generic redirect refusal; public `get()`).
- **L2, L4–L8, M3, I3** — `5c42bf43` (constant-time compare; provider_ref quote+validate; ISO-4217
  Decimal currency module; PENDING_SETTLEMENT→pending; amount/metadata/currency validation;
  return_url documented).
- **L3, M6** — `0990b7c9` (SECRETS_KEK/BACKUP_KEK boot guard; background reconcile/expiry sweep,
  off by default).

**Still open:**
- **M7** (refund / chargeback / dispute ingestion) — DEFERRED: spans port + adapter + state as a
  feature; to be built as its own pass (or with Phase 6), not squeezed into the parallel remediation.
- **I1** (internal-token is omnipotent; use the `jwks`/`static` per-caller identity in production) —
  a deployment/provisioning choice for Phase 6, not a code change here.
</content>
