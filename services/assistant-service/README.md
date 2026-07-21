# assistant-service

A help assistant that answers from a knowledge base you control — **grounded and cited, or an
honest abstention.** It ships its own drop-in UI, and it runs with no API key and no extra
infrastructure.

## Why it is built this way

**It retrieves rather than stuffing the prompt.** The tempting shortcut is to put the whole KB in
every request. A help KB big enough for an admin console runs to hundreds of entries — six figures
of tokens per message — so that design forces a byte cap and freezes your help at whatever fits.
Here prompt size is independent of KB size: a few ranked passages, whether the KB holds fifty
entries or fifty thousand.

**The facts are yours, and callers cannot supply them.** There is deliberately no `kb` field on the
ask request. Whoever controls an assistant's knowledge controls what your product appears to assert.

**It works with nothing configured.** The default model driver is *extractive*: it answers by
quoting the passages retrieval found, with citations. No API key, no vendor, and it cannot
hallucinate because nothing is generated. Configuring a provider buys phrasing, not correctness.

## Quick start

```bash
KB_SOURCE_DRIVER=file KB_FILE_PATH=./kb.json uvicorn app.main:app
```

A first KB is ids, titles and text:

```json
{
  "billing.vat": {
    "title": { "en": "VAT number", "ar": "الرقم الضريبي" },
    "body":  { "en": "Your VAT number is 15 digits...", "ar": "..." }
  }
}
```

Only `title` and `body` are required; a bare string works instead of a language map.

## The entry shape

| field | what it buys |
| --- | --- |
| `title`, `body` | **required** — language maps, or a bare string |
| `kind` | free-form label (`glossary`, `field`, `howto`, `policy`, `action`) |
| `surfaces` | which of your UIs it belongs to. **Omit it and every surface gets it** |
| `anchor` | ties it to a UI element, so a `?` icon fetches it directly — no ranking, no model, no cost |
| `requires` | a capability. The entry is invisible without it |
| `related` | other entry ids |

**Several UIs, one KB.** Tag entries with `surfaces` rather than keeping a KB file per app: shared
concepts (your glossary) are authored **once**, and copies cannot drift.

**Surface scopes relevance; capability scopes visibility.** A client declares its own surface, so
surface is a hint. `requires` is the control — an admin entry stays invisible to a caller who
merely claims to be the admin app.

## The two read paths

```
GET  /api/v1/assistant/help?anchor=settings.billing.vat   # the ? icon — direct lookup
POST /api/v1/assistant/ask  {question, surface, lang}     # ranked retrieval + answer
```

`ask` returns the answer, its `citations`, any `actions` it can link to, and `redacted` — what was
stripped from the question before anything saw it.

## The widget

```html
<script src="https://your-assistant/widget.js"></script>
<assistant-widget surface="admin" locale="ar"
                  base-url="https://your-assistant" token="..."></assistant-widget>
```

A web component: identical in React, Vue, Angular or plain HTML, so four UIs share one widget. It
renders into a shadow root, so your CSS cannot break it and its CSS cannot leak into your page.
Zero dependencies. It holds **no secret** — your app mints a short-lived token and passes it in.

Branding (colours, logo, name, greeting, position) is server-side config per tenant and per surface,
and every value is validated: colours must be hex, URLs must be `http(s)`, text may not contain
markup. Arabic mirrors the whole panel, not only the strings.

## Help that writes itself

Point `KB_SOURCE_DRIVER=catalog` at an aggregated action catalog and every declared action becomes
an entry, with the route's own capability as its `requires`. Help generated from a declaration
cannot go stale, and it is tied to no particular platform: any service declaring itself in that
shape appears automatically. Use `composite` to merge authored and generated help — the first source
wins, so put your own file first and you can override one generated sentence without forking
anything.

## Privacy and cost

- **Redaction is on by default**, before the model, before storage, before ranking. People paste
  card numbers into help boxes when a payment fails.
- **Conversations are opt-in** (`CONVERSATIONS_ENABLED`), tenant- and subject-scoped, encrypted at
  rest, erased through the standard DSAR contract. Retention is enforced by a nightly sweeper, not
  merely configured.
- **A daily quota** per subject *and* per tenant, so one heavy user cannot exhaust their colleagues'
  day. An abstention is refunded — you should not burn budget discovering a gap in your KB.

## Honest limits

- **Lexical ranking misses paraphrases.** BM25 is the zero-infra default; set
  `EMBEDDINGS_DRIVER=http` for semantic scoring. The two are blended, so an exact term still wins on
  exactness.
- **Arabic is normalised, not stemmed.** Diacritics, alef and ya variants and tatweel are folded;
  root-and-pattern morphology is not attempted, because half-done stemming loses recall while
  appearing to add it.
- **Answer quality is bounded by your KB.** If it does not hold the answer, the correct outcome is
  an abstention rather than a better guess — and `tests/eval/` measures whether it abstains when it
  should, including one case it currently gets wrong, recorded rather than removed.

---

## Principles (inherited, non-negotiable)

- **100% self-contained** — no shared cross-service library. Clone and go.
- **Zero-hardcoded** — every host/limit/secret comes from env (`app/config.py`).
- **Fail-fast config** — invalid/missing env → the process refuses to start.
- **Secure by default** — hardened headers, deny-by-default user auth, safe errors,
  redacted logs.
- **Runs with zero infra** — no database/Redis needed to boot.
- **Permissive licenses only** (Apache-2.0 here; deps MIT/BSD/Apache/ISC).

---

## Endpoints

| Path | Tier | Auth |
|------|------|------|
| `GET /healthz` | public | none |
| `GET /readyz` | public | none |
| `GET /metrics` | public | none (Prometheus) |
| `GET /internal/health` | internal | `X-Internal-Token` |
| `GET /internal/manifest` | internal | `X-Internal-Token` |
| `GET /api/v1/*` | user | deny-by-default (real auth in Phase 3) |

---

## Run it

Requires **Python 3.12**.

```bash
# 1. create a 3.12 virtual env (Windows: use the py launcher)
py -3.12 -m venv .venv
. .venv/Scripts/activate          # PowerShell: .venv\Scripts\Activate.ps1
#   (Linux/macOS: python3.12 -m venv .venv && source .venv/bin/activate)

# 2. install (runtime + dev)
pip install -e ".[dev]"

# 3. configure
cp .env.example .env
#   set a strong INTERNAL_SERVICE_TOKEN (>= 32 chars):
python -c "import secrets; print(secrets.token_urlsafe(48))"

# 4. run
uvicorn app.main:app --host 0.0.0.0 --port 8000

# 5. test
pytest -q
```

---

## Clone into a new service

Until the `create-service` scaffolder lands (Phase 5), copy this folder and
replace the `assistant-service` placeholders + `pyproject.toml` name, then add your
business routes under `app/api/v1/` (each with `Depends(require_user)`).

---

## Ports & adapters (Phase 2)

Every external dependency sits behind a **port** (interface). Pick an adapter per
port with a `*_DRIVER` env var — **swap by config, never by code**. Defaults are
**zero-infra** (the service boots with nothing installed).

| Port | `*_DRIVER` | Adapters (default first) | Extra needed |
|------|-----------|--------------------------|--------------|
| datastore | `DATASTORE_DRIVER` | `memory`, `postgres` | `[postgres]` |
| cache | `CACHE_DRIVER` | `memory`, `redis` | `[redis]` |
| event_bus | `EVENTBUS_DRIVER` | `memory`, `redis` | `[redis]` |
| queue | `QUEUE_DRIVER` | `memory`, `redis` | `[redis]` |
| object_store | `OBJECTSTORE_DRIVER` | `local`, `memory`, `s3` | `[s3]` |
| secrets | `SECRETS_DRIVER` | `env`, `file`, `vault` | `[vault]` |
| token_verifier | `TOKEN_VERIFIER_DRIVER` | `null` (deny), `hs256`, `rs256`, `jwks` | — |
| rate_limiter | `RATELIMITER_DRIVER` | `memory`, `redis` | `[redis]` |
| telemetry | `TELEMETRY_DRIVER` | `stdout` | — |

Infra adapters import their driver **lazily** — selecting one without its extra
gives a clear error (`pip install "assistant-service[redis]"`), never a crash.
The wired ports are listed at `GET /internal/manifest`.

### How to add a new adapter (3 steps, no core edits)

1. **Implement the port Protocol** in `app/adapters/<port>/<name>.py` (set `driver = "<name>"`; lazy-import any optional dep inside `__init__`).
2. **Register it** — add one line to that port's registry in `app/container.py`:
   ```python
   _CACHE = { "memory": ..., "redis": ..., "mynew": lambda s: MyNewCache(s.CACHE_URL) }
   ```
3. **Pass the contract** — run it through `tests/contracts/test_<port>_contract.py` (add your factory to the fixture `params`). Green = done.

## Security & CI gates

Production-ready: security rules are enforced by an automatic gate that blocks
the build on any breach. Run it locally or in CI.

```bash
pip install -e ".[dev,ci]"
python ci/security_gate.py            # local: offline gates enforced
python ci/security_gate.py --strict   # CI: nothing may be skipped
# or: make security   /   make check   (test + strict gate)
```

| Gate | Tool | Blocks on |
|------|------|-----------|
| License | `ci/check_licenses.py` (stdlib) | any non-permissive dep (GPL/AGPL/SSPL/BSL/… or unknown) |
| SAST | Bandit | Medium/High code weakness |
| Secret scan | detect-secrets | a new secret vs `.secrets.baseline` |
| Dependency CVE | pip-audit | a known vulnerability (network) |
| SBOM | CycloneDX | (produces `sbom.cdx.json`) |
| Container/fs | Trivy | HIGH/CRITICAL vuln or misconfig (if installed) |

**No silent gaps:** a gate that can't run (offline / tool missing) is reported as
`SKIP` with a reason; `--strict` (CI) turns SKIP into FAIL. GitHub Actions
(`.github/workflows/security.yml`) runs `pytest` + the strict gate on every push/PR.
The hardened `Dockerfile` (slim, non-root, read-only-friendly) backs the container scan.

## Layout

```
app/
  config.py              # Pydantic settings (fail-fast, zero-hardcoded)
  container.py           # composition root: port -> adapter registry (allowlist)
  main.py                # app factory + lifespan (builds container)
  manifest.py            # self-describing descriptor (/internal/manifest)
  observability/logging.py   # JSON logs + redaction
  middleware/            # request-id, security headers, timeout, body-limit
  ports/                 # the 9 interfaces (Protocols) + errors
  adapters/<port>/       # implementations (memory + real; infra ones lazy)
  api/
    errors.py            # safe, generic error handlers
    health.py            # /healthz /readyz (adapter probes)
    metrics.py           # /metrics
    internal.py          # /internal/* (X-Internal-Token, timing-safe)
    deps.py              # get_container, require_user (deny-by-default stub)
    v1/                  # /api/v1/* business routes
tests/                   # unit / integration / security / contracts
```

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
