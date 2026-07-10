# Phase 2 — Ports & Adapters (detailed steps)

> **Goal:** turn the Phase 1 skeleton into a true **Hexagonal / Ports & Adapters**
> service. Define the swappable **port interfaces**, ship a **memory/null adapter**
> for every one (so the service still boots with **zero infra**), add the
> **zero-infra real adapters** that need no external system, and build the
> **config-driven registry** (`container.py`) that wires port → adapter from env —
> with an allowlist and **fail-fast on unknown drivers**.
>
> _Reference: `docs/architecture-plan.md` §3 (hexagonal + layout), §4 (the 9 ports), §7 (config-driven wiring)._
> _Rules that apply: self-contained · zero-hardcoded · fail-fast config · runs-with-zero-infra · secure-by-default · permissive licenses._

---

## 0. Definition of done (Phase 2)

Phase 2 is complete when **all** are true:

- [ ] All **9 ports** exist as typed interfaces (`Protocol`/`ABC`) in `app/ports/`.
- [ ] **Every port has a memory/null adapter**; the service boots and all tests pass with **zero external infra**.
- [ ] **Zero-infra real adapters** work: secrets (`env`,`file`), token-verifier (`hs256`,`rs256`), object-store (`local`), rate-limiter (`memory`), telemetry (`stdout`).
- [ ] **Infra-gated real adapters** are present but **lazy** (driver imported only when selected) and **config-gated**: datastore `postgres`, cache/eventbus/queue `redis`, object-store `s3`, secrets `vault`, token-verifier `jwks`. Selecting one without its optional dep gives a **clear error**, never a crash-on-import.
- [ ] **`container.py`** builds each port from a `*_DRIVER` env value via a **registry + allowlist**; an unknown driver is rejected at boot (**fail-fast `ConfigError`**).
- [ ] **Zero-hardcoded** — driver names, URLs, and credentials come only from config.
- [ ] Adapters that can report health are registered as **readiness probes**; `manifest.ports` lists `{port, driver}`.
- [ ] **Contract test suite per port** — one behavioural spec every adapter must satisfy; the zero-infra adapters pass it in CI.
- [ ] **Hardening tests** pass: registry fail-fast, secrets redaction, token alg-confusion/expiry/bad-sig rejected, object-store path-traversal rejected, rate-limiter enforced.
- [ ] README documents the ports, the env knobs, and **how to add a new adapter**.

---

## 1. Prerequisites & decisions (confirm before coding)

1. **Optional dependencies** — real infra adapters use extras so the base stays lean:
   `[project.optional-dependencies]` groups like `postgres = ["asyncpg"]`, `redis = ["redis"]`,
   `s3 = ["aioboto3"]`, `jwt = ["pyjwt[crypto]"]`, `vault = ["hvac"]`. **All must be permissive** (BSD/MIT/Apache). Confirm each.
2. **Default drivers = zero-infra.** Every `*_DRIVER` defaults to `memory`/`null`/`env`/`local` so a fresh clone runs with nothing installed.
3. **Async everywhere.** Ports are `async` where I/O is involved (datastore/cache/queue/objectstore/eventbus); pure-CPU ports (token-verifier, rate-limiter) may be sync.
4. **Contract tests are the source of truth** for adapter behaviour — an adapter is "done" when it passes its port's contract suite.

---

## 2. Steps

### Step 2.1 — Config: driver knobs (zero-hardcoded, zero-infra defaults)
- Extend `app/config.py` with a `*_DRIVER` field per port + adapter-specific optional settings:
  - `DATASTORE_DRIVER=memory` (`+ DATASTORE_DSN` optional), `CACHE_DRIVER=memory` (`+ CACHE_URL`),
    `EVENTBUS_DRIVER=memory`, `QUEUE_DRIVER=memory`, `OBJECTSTORE_DRIVER=local` (`+ OBJECTSTORE_ROOT`/`_BUCKET`),
    `SECRETS_DRIVER=env` (`+ SECRETS_FILE`/`VAULT_*`), `TOKEN_VERIFIER_DRIVER=hs256` (`+ keys/JWKS_URL/ISSUER/AUDIENCE`),
    `RATELIMITER_DRIVER=memory`, `TELEMETRY_DRIVER=stdout`.
- All optional; defaults reach **no external system**.
- Update `.env.example` with every new knob, documented.
- **Check:** app still boots with none of them set (all defaults).

### Step 2.2 — Port interfaces (`app/ports/`)
- One module per port defining a typed `Protocol` (or `ABC`) + a small result/exception vocabulary:
  `datastore.py`, `cache.py`, `event_bus.py`, `queue.py`, `object_store.py`,
  `secrets_provider.py`, `token_verifier.py`, `rate_limiter.py`, `telemetry.py`.
- Shared `app/ports/errors.py`: `PortError`, `ConfigError`, `NotFound`, `Unavailable`.
- Each port declares an optional `async def health() -> bool` where meaningful.
- Interfaces are **minimal and generic** — no domain concepts, no vendor terms.
- **Check:** `mypy`/`ruff` clean; importing `app.ports.*` pulls in **no** third-party driver.

### Step 2.3 — Memory / null adapters (`app/adapters/<port>/memory.py`)
- A fully-working in-process implementation of every port:
  memory datastore (dict + optional tenant scoping hook), memory cache (with TTL),
  in-proc event bus (async pub/sub), in-memory queue, in-memory object store,
  env-less null telemetry, etc.
- These make the service **runnable and testable with zero infra**.
- **Check:** each memory adapter passes its contract suite (Step 2.8).

### Step 2.4 — Zero-infra real adapters
- Adapters that need **no external system**, proving swappability today:
  - `secrets/env.py`, `secrets/file.py` (read a KV file; env always wins — mirrors ammar's fail-open Vault seam, generalized).
  - `token_verifier/hs256.py`, `token_verifier/rs256.py` (verify with pinned alg + issuer + audience; **reject alg confusion**).
  - `object_store/local.py` (filesystem; **path-traversal-safe** keys, CWE-22).
  - `rate_limiter/memory.py` (token-bucket / fixed-window).
  - `telemetry/stdout.py` (wraps the Phase 1 JSON logger + Prometheus).
- **Check:** all pass contract + hardening tests, no infra.

### Step 2.5 — Infra-gated real adapters (lazy, config-gated)
- Provide real adapters for external systems, but **import the driver lazily** inside the constructor so importing the module never fails when the optional dep is absent:
  `datastore/postgres.py` (asyncpg), `cache/redis.py`, `event_bus/redis.py`, `queue/redis.py`,
  `object_store/s3.py` (aioboto3), `secrets/vault.py` (hvac), `token_verifier/jwks.py`.
- If selected without its dep installed → raise a **clear `ConfigError`** ("driver 'postgres' needs the 'postgres' extra: pip install …"), never an ImportError crash.
- **Check:** selecting an infra driver without its extra yields the friendly error; with a fake/mocked driver the contract suite passes.

### Step 2.6 — Registry & container (`app/container.py`)
- A per-port **registry dict** `{driver_name: builder}` (the **allowlist** — unknown driver is impossible to construct).
- `build_container(settings) -> Container` resolves every port from its `*_DRIVER`, raising `ConfigError` (fail-fast) on any unknown driver.
- `Container` is a frozen holder exposing `.datastore`, `.cache`, … and an `async close()`.
- Wire into `app/main.py` **lifespan**: build the container on startup (store on `app.state`), `await container.close()` on shutdown.
- A `get_container(request)` FastAPI dependency exposes ports to routes.
- **Check:** unknown `CACHE_DRIVER=bogus` → app refuses to boot with a clear message.

### Step 2.7 — Readiness & manifest integration
- For each resolved adapter with `health()`, register a readiness probe (Phase 1 `register_readiness_check`) so `/readyz` reflects real dependency health.
- Populate `manifest.ports` = `[{"port": "datastore", "driver": "memory"}, …]` from the container.
- **Check:** `/readyz` still 200 with memory adapters; `/internal/manifest` lists the wired ports+drivers.

### Step 2.8 — Contract test suites (`tests/contracts/`)
- One reusable, adapter-agnostic behavioural suite per port (e.g. `datastore_contract.py`) expressed as a base test class or parametrized fixture.
- Run each **zero-infra** adapter through its port's contract in CI (memory/local/env/hs256/rs256).
- Infra adapters can opt into the same suite behind an env flag when infra is available (skipped in CI otherwise, with a `log`/skip reason — **no silent gaps**).
- **Check:** every zero-infra adapter is green against its contract.

### Step 2.9 — Hardening / security tests (`tests/security/`)
- Registry **fail-fast** on unknown driver (`ConfigError`).
- Secrets adapters never log values; **env wins** over file.
- Token-verifier: reject **alg confusion** (e.g. `none`/HS-with-RS-key), expired, bad-signature, wrong issuer/audience.
- Object-store: reject keys with `..`, absolute paths, NUL (CWE-22).
- Rate-limiter: enforces the limit; independent buckets per key.
- **Check:** all pass.

### Step 2.10 — Docs & extensibility
- README: ports table, env knobs, **"How to add a new adapter"** (implement the Protocol → register in the port's registry → pass the contract suite). Zero core edits required beyond one registry line.
- **Check:** a reader can add an adapter following only the README.

### Step 2.11 — Full verification
- Boot **zero-infra** → `pytest` green (unit + contract + security).
- Live boot with a couple of non-default zero-infra drivers set (e.g. `SECRETS_DRIVER=file`, `TOKEN_VERIFIER_DRIVER=rs256`) → healthz/readyz/manifest correct.
- **Check:** all green; no infra needed.

---

## 3. Out of scope (deferred — do NOT build in Phase 2)

- **Applying `TokenVerifier` to `/api/v1` auth, RBAC/capabilities, `CweGuard` + `cwe.json`** → Phase 3.
- **CI security gates (SAST, CVE scan, secret scan, license CI, SBOM, container scan)** → Phase 4.
- **`create-service` generator (adapter-picking, self-verifying)** → Phase 5 (it consumes this phase's ports/adapters).
- **Full production drivers for every backend** (kafka/nats/gcs/azure) — add per demand; the interface + registry make them drop-in.

---

## 4. Execution checklist (tick as we build)

> **Sync rule:** each item is a task in the task list. When a task is completed, mark it `completed`
> in the task list **and** tick its box here.

| Task | Step | Status |
|---|---|---|
| #13 | 2.1 Config: driver knobs | ✅ completed |
| #14 | 2.2 Port interfaces (9) | ✅ completed |
| #15 | 2.3 Memory/null adapters | ✅ completed |
| #16 | 2.4 Zero-infra real adapters | ✅ completed |
| #17 | 2.5 Infra-gated real adapters (lazy) | ✅ completed |
| #18 | 2.6 Registry & container | ✅ completed |
| #19 | 2.7 Readiness & manifest integration | ✅ completed |
| #20 | 2.8 Contract test suites | ✅ completed |
| #21 | 2.9 Hardening/security tests | ✅ completed |
| #22 | 2.10 Docs & extensibility | ✅ completed |
| #23 | 2.11 Full verification | ✅ completed |
| —   | DoD (§0) all checked | ✅ completed (83/83 tests) |

_Legend: ⬜ pending · 🟡 in-progress · ✅ completed._

---

## 5. Why this is powerful (design notes)

- **Swap by config, not code** — change one `*_DRIVER` env value to move from memory → Postgres/Redis/S3. Core logic never changes.
- **Allowlist registry** — only known adapters are constructable; an unknown driver can't be injected (defends config-injection).
- **Contract tests** — every adapter proves the same behaviour, so swapping backends is safe by construction (ISO/ASVS-grade discipline).
- **Lazy infra imports** — the base image stays tiny and dependency-light; you install only the extras you use (smaller attack surface, fewer CVEs).
- **Zero-infra default** — a brand-new service boots and is fully testable with nothing installed, then scales up by config.

_When every box is ✅, Phase 2 is done. Next: `docs/phases/phase-3-auth-authz-cwe-guard.md`._
