# webhook-service

> Self-contained, **general & plug-and-play** webhook service (SaaS checklist §10):
> **outbound** subscriptions with signed, retried, circuit-broken, dead-lettered delivery;
> **inbound** signed ingest with replay protection; per-endpoint health monitoring.
> Backend only. Zero new runtime deps (`httpx` transport is an optional `[http]` extra).

---

## The part the checklist doesn't mention: SSRF (CWE-918)

Customers register **arbitrary URLs** and *we* make the server-side request. Without a
guard, `http://169.254.169.254/…` steals our cloud credentials and `http://127.0.0.1:6379`
talks to our Redis. So `core/ssrf.py` on **every attempt**:

1. requires `https` (configurable), rejects `file://`/`gopher://`/… and URL credentials;
2. **resolves DNS, then requires every resolved address to be a public unicast IP** —
   blocks private, loopback, link-local, multicast, reserved, and **cloud-metadata**
   (`169.254.169.254`, `fd00:ec2::254`, Alibaba/Oracle) ranges;
3. **pins the connection to that validated IP** — the transport never re-resolves, which
   closes the DNS-rebinding window between "we checked" and "we connected";
4. re-validates on **each retry** (DNS can change between attempts).

`allow_private_ips` (dev only) never unlocks metadata. Verified by a parametrised attack
matrix.

## Ports

| Port | Drivers | Role |
|------|---------|------|
| `WebhookTransport` | `stdlib` · `httpx` (`[http]`) · `recording` (no network) · `null` | POST to the **pinned IP**, size-capped read |
| `Signer` | `hmac-sha256` (Stripe-style `t=…,v1=…`) · `null` (fail-closed) | sign outbound / verify inbound |

Reuses `Cache` (hot path), `Queue`, `EventBus`, `DataStore`.

## Outbound delivery guarantees

- **At-least-once, dedupe-friendly** — a **stable `event_id`** is sent as `Idempotency-Key`
  and `X-Webhook-Id`, identical on every retry. Consumers dedupe on it.
- **Signed exact bytes** — the body is serialized once, signed with a timestamp, and that
  same buffer goes on the wire. Verify is constant-time; endpoints hold `[primary, previous]`
  secrets so rotation never drops a beat.
- **Smart classification** — `2xx` delivered · `4xx` terminal **except 408/429** ·
  `5xx`/timeout/DNS retryable. A blind retry on a `400` would hammer a customer forever.
- **Full-jitter backoff** — `uniform(0, min(base·2ⁿ, cap))`, deterministic per delivery.
  Plain `base·2ⁿ` synchronises every failed delivery into a thundering herd.
- **Circuit breaker per endpoint** — after N consecutive failures the circuit opens;
  deliveries stay **queued (never dropped)**; a half-open probe closes it on success.
- **Dead-letter + replay** — exhausted retries land in the DLQ (never discarded);
  `POST /deliveries/{id}/replay` and `/events/{id}/replay` re-drive them.

## Inbound ingest (`POST /inbound/{source}`)

The **HMAC signature is the authentication** (no bearer token). Hot path: verify → dedupe
→ one write → publish to the EventBus → `200`.

- **Replay protection** — the signature covers a timestamp; anything outside the tolerance
  window is rejected.
- **Dedupe on the provider's event id**, never a body hash — two distinct events can carry
  byte-identical bodies. A bounded window (`INBOUND_DEDUPE_TTL_SECONDS`) keeps the table small.
- A bad signature → **400, never 5xx** (a 5xx makes the provider retry a payload we'll
  never accept). Unknown source / null signer → **fail-closed**.

## Hot path (runs on every event)

`dispatch()` resolves subscribers via a **cached index** (`subs:<gen>:<tenant>:<type>`),
invalidated only on endpoint writes — circuit/stat updates don't bust it. Benchmarks assert
**0 scans / 0 reads** for the lookup after warm; inbound ingest does **exactly one write**.
A guard test fails if any `Settings` knob has no consumer.

## Endpoints

| Tier | Method · Path | Purpose |
|------|---------------|---------|
| internal | `POST /internal/webhooks/dispatch` | fan an event out to subscribers (hot path) |
| internal | `POST /internal/webhooks/deliver/{id}` · `/sweep` | worker: deliver / retry-sweep |
| public | `POST /inbound/{source}` | signed inbound ingest (signature = auth) |
| user | `POST/GET /api/v1/webhooks/endpoints` · `PATCH/DELETE /{id}` | manage endpoints |
| user | `POST /endpoints/{id}/rotate-secret` · `GET /{id}/health` | rotate / health |
| user | `GET /deliveries` · `/dead-letters` | delivery log + DLQ (monitoring) |
| user | `POST /deliveries/{id}/replay` · `/events/{id}/replay` | replay |

Caps: `platform.webhooks` (cross-tenant) · `webhooks.manage` (own tenant) · `webhooks.read`.

## Run

```bash
py -3.12 -m venv .venv && .venv/Scripts/python -m pip install -e ".[dev,ci]"
uvicorn app.main:app
pytest -q ; python ci/security_gate.py
```

## Out of scope
Third-party connector catalogue (ERP/CRM) → connector-service · developer portal/OpenAPI
sandbox → UI · API-key issuance → identity · request routing → api-gateway.

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
